from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field


TASK_STATUS_DESC = (
    "任务状态固定值：`idle`=未找到任务或当前无进行中任务；`starting`=启动中；"
    "`recording`=录制中；`stopping`=停止中；`finished`=已完成并保留文件；"
    "`cancelled`=已取消且本地文件已删除；`failed`=失败；`interrupted`=服务异常中断。"
)
_log = logging.getLogger("edge.mobile_record.routes")


def _mask_secret(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 2:
        return "*" * len(text)
    return f"{text[:1]}***{text[-1:]}(len={len(text)})"


class MobileRecordClassroomItem(BaseModel):
    classroomId: str = Field(default="", description="教室ID。由服务端传入并原样返回，用于定位具体教室。")
    selectable: bool = Field(default=True, description="该教室当前是否可选择并发起录制。固定值：`true`=可选择；`false`=已有进行中的录制，不能选择。前端判断是否可录制只看这个字段。")
    taskId: str = Field(default="", description="占用该教室的录制任务ID。教室可选时为空字符串。")
    recordUserId: str = Field(default="", description="当前录制人ID。教室可选时为空字符串。")
    recordUserName: str = Field(default="", description="当前录制人姓名。教室可选时为空字符串。")


class MobileRecordClassroomStatusData(BaseModel):
    items: list[MobileRecordClassroomItem] = Field(default_factory=list, description="教室状态列表。")


class MobileRecordTaskStatusData(BaseModel):
    taskId: str = Field(default="", description="移动录制任务ID。")
    status: str = Field(default="", description=TASK_STATUS_DESC)
    classroomId: str = Field(default="", description="教室ID。")
    recordUserId: str = Field(default="", description="录制人ID。")
    recordUserName: str = Field(default="", description="录制人姓名。")
    startTime: str = Field(default="", description="录制开始时间，ISO 字符串。")
    finishTime: str = Field(default="", description="实际结束时间，ISO 字符串；未结束时为空。")
    elapsedSeconds: int = Field(default=0, description="已录制时长，单位：秒。")
    remainingSeconds: int = Field(default=0, description="距离预计自动停止还剩多少秒，单位：秒。非录制中任务为 0。")
    playUrl: str = Field(default="", description="录制完成后的 HLS 播放地址。`status=finished` 时返回可播放地址；未完成、取消或失败时为空字符串。")
    segmentCount: int = Field(default=0, description="HLS ts 分片数量。")
    fileSize: int = Field(default=0, description="HLS 分片文件总大小，单位：字节。")
    durationSeconds: float = Field(default=0.0, description="最终 HLS 可播放时长，单位：秒。")
    codec: str = Field(default="", description="视频编码。常见值：`h264`、`hevc`；无法探测时为空字符串。")
    errorMessage: str = Field(default="", description="失败或中断原因。没有错误时为空字符串。")


class MobileRecordTaskListData(BaseModel):
    items: list[MobileRecordTaskStatusData] = Field(default_factory=list, description="移动录制任务列表。")


class MobileRecordStartData(BaseModel):
    taskId: str = Field(default="", description="移动录制任务ID。")
    status: str = Field(default="", description="开始后的任务状态。固定值通常为 `recording`=录制中。")
    recordUserId: str = Field(default="", description="录制人ID。")
    estimatedDurationSeconds: int = Field(default=0, description="预计录制时长，单位：秒。")
    elapsedSeconds: int = Field(default=0, description="已录制时长，单位：秒，刚开始时通常为 0。")
    remainingSeconds: int = Field(default=0, description="距离预计自动停止还剩多少秒，单位：秒。")


class MobileRecordStopData(BaseModel):
    taskId: str = Field(default="", description="移动录制任务ID。")
    status: str = Field(default="", description="停止后的任务状态。固定值：`finished`=正常结束并保留文件；`cancelled`=取消并删除本地文件；`failed`=停止后文件无效或处理失败。")
    playUrl: str = Field(default="", description="录制完成后的 HLS 播放地址。取消或失败时为空字符串。")
    segmentCount: int = Field(default=0, description="最终 HLS ts 分片数量。")
    fileSize: int = Field(default=0, description="HLS 分片文件总大小，单位：字节。")
    durationSeconds: float = Field(default=0.0, description="最终 HLS 可播放时长，单位：秒。")
    codec: str = Field(default="", description="视频编码。常见值：`h264`、`hevc`；无法探测时为空字符串。")
    errorMessage: str = Field(default="", description="失败原因。没有错误时为空字符串。")


class MobileRecordExtendData(BaseModel):
    taskId: str = Field(default="", description="移动录制任务ID。")
    status: str = Field(default="", description="延长后的任务状态。固定值通常为 `recording`=录制中；如果任务不存在或状态不允许延长，则本接口会返回失败 code。")
    extendDurationSeconds: int = Field(default=0, description="累计已延长时长，单位：秒。")
    remainingSeconds: int = Field(default=0, description="延长后距离预计自动停止还剩多少秒，单位：秒。")
    maxEndTime: str = Field(default="", description="延长后的预计自动停止时间，ISO 字符串。")


class MobileRecordClassroomStatusResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="业务结果码。固定值：`SUCCESS`=成功；`FAILED`=失败。")
    message: str = Field(default="查询成功", description="业务提示信息。成功固定为“查询成功”；失败时返回具体原因，例如查询教室录制占用状态失败。")
    data: MobileRecordClassroomStatusData = Field(..., description="教室录制占用状态。")


class MobileRecordStartResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="启动结果码。固定值：`SUCCESS`=启动成功；`FAILED`=启动失败。")
    message: str = Field(default="启动录制成功", description="启动结果说明。`code=SUCCESS` 时固定为“启动录制成功”；`code=FAILED` 时按实际原因返回：磁盘已满或剩余空间不足、录像设备取流失败、接口服务未启动、录制进程或依赖服务启动失败、任务ID重复、教室已被占用、当前通道/教室已有录制任务、录制人有进行中的录制任务、录制参数错误或其他原因。")
    data: MobileRecordStartData = Field(..., description="开始录制后的必要信息。")


class MobileRecordStopResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="业务结果码。固定值：`SUCCESS`=成功；`FAILED`=失败。")
    message: str = Field(default="停止录制成功", description="业务提示信息。成功时根据 action 返回“停止录制成功”或“取消录制成功”；失败时按实际原因返回：参数错误、录制任务不存在、停止后文件不可用或其他停止失败原因。")
    data: MobileRecordStopData = Field(..., description="停止或取消后的必要信息。")


class MobileRecordExtendResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="业务结果码。固定值：`SUCCESS`=成功；`FAILED`=失败。")
    message: str = Field(default="延长录制成功", description="业务提示信息。成功固定为“延长录制成功”；失败时按实际原因返回：录制任务不存在、任务不是录制中不能延长或其他延长失败原因。")
    data: MobileRecordExtendData = Field(..., description="延长录制后的必要信息。")


class MobileRecordStatusResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="业务结果码。固定值：`SUCCESS`=成功；`FAILED`=失败。")
    message: str = Field(default="查询成功", description="业务提示信息。成功固定为“查询成功”；失败时返回具体原因，例如查询移动录制任务状态失败。")
    data: MobileRecordTaskStatusData = Field(..., description="任务状态详情。")


class MobileRecordListResponse(BaseModel):
    code: str = Field(default="SUCCESS", description="业务结果码。固定值：`SUCCESS`=成功；`FAILED`=失败。")
    message: str = Field(default="查询成功", description="业务提示信息。成功固定为“查询成功”；失败时返回具体原因，例如查询移动录制任务列表失败。")
    data: MobileRecordTaskListData = Field(..., description="任务列表数据。")


class MobileRecordStartRequest(BaseModel):
    taskId: str = Field(..., description="格式：string。服务端生成的移动录制任务ID。边缘服务内部必须唯一，用它串起开始、停止、延长、状态查询和最终回调。示例：`record_20260605_001` 或 `17`。")
    campusCode: str = Field(default="", description="格式：string。校区编码，含义与 `/api/stream/open` 相同，用于边缘服务校区校验；没有校区编码时可传空字符串。示例：`NJ`。")
    classroomId: str = Field(..., description="格式：string。教室ID。用于开始录制前的教室占用校验；同一教室同一时间只允许一个进行中的录制任务。示例：`room_301`。")
    nvrDeviceId: int = Field(..., description="格式：int。NVR设备ID，含义与 `/api/stream/open` 相同。示例：`10001`。")
    ipAddress: str = Field(..., description="格式：string。NVR设备IP地址或域名，含义与 `/api/stream/open` 相同。示例：`192.168.9.83`。")
    port: int = Field(default=554, description="格式：int。NVR RTSP端口，含义与 `/api/stream/open` 相同，默认 `554`。如果服务端给的是映射端口，也传映射后的 RTSP 端口。")
    account: str = Field(default="", description="格式：string。NVR登录账号，含义与 `/api/stream/open` 相同。示例：`admin`。")
    password: str = Field(default="", description="格式：string。NVR登录密码，含义与 `/api/stream/open` 相同。")
    nvrChannelId: str = Field(default="", description="格式：string。通道ID标识，含义与 `/api/stream/open` 相同。可为空字符串；为空时边缘服务主要使用 nvrChannelNum 定位通道。示例：`cam_back_301`。")
    nvrChannelNum: int = Field(..., description="格式：int。业务通道号，含义与 `/api/stream/open` 相同；移动录制会录制这个通道。示例：`4`。")
    estimatedDurationSeconds: int = Field(default=1800, ge=60, description="格式：int，单位：秒。预计录制时长，最小 `60`，当前边缘服务会限制最大 6 小时。例如 `1800`=30分钟，`3600`=1小时。到达该时长后，边缘服务会自动结束录制。")
    recordUserId: str = Field(..., description="格式：string。录制人ID。用于录制人占用判断；同一个录制人同一时间只允许一个进行中的录制任务。示例：`u_001`。")
    recordUserName: str = Field(default="", description="格式：string。录制人姓名。教室状态接口会返回这个字段，用于展示“谁正在录制”；没有姓名时可传空字符串。示例：`张老师`。")
    callbackUrl: str = Field(default="", description="格式：string，HTTP/HTTPS URL，可为空字符串。该字段已废弃，当前版本边缘服务会忽略它，并统一使用本机配置中的 `serverAddress` 自动拼接默认回调地址：`{serverAddress}/api/v1/record-task/callback`。保留这个字段仅用于兼容旧服务端。")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "record_20260605_001",
                "campusCode": "NJ",
                "classroomId": "room_301",
                "nvrDeviceId": 10001,
                "ipAddress": "192.168.9.83",
                "port": 554,
                "account": "admin",
                "password": "******",
                "nvrChannelId": "cam_back_301",
                "nvrChannelNum": 4,
                "estimatedDurationSeconds": 3600,
                "recordUserId": "u_001",
                "recordUserName": "张老师",
                "callbackUrl": "",
            }
        }
    }


class MobileRecordStopRequest(BaseModel):
    taskId: str = Field(..., description="格式：string。要停止的移动录制任务ID，必须与 start 接口传入的 taskId 一致。示例：`record_20260605_001`。")
    action: Literal["finish", "cancel"] = Field(default="finish", description="格式：string enum。固定值：`finish`=正常结束并保留录制文件；`cancel`=取消录制并删除本地文件。")
    operatorUserId: str = Field(default="", description="格式：string。发起停止操作的人ID，可为空字符串。默认场景是录制人本人操作；如果服务端代操作，可传实际操作人ID用于日志追踪。")
    stopReason: Literal["manual", "cancel"] = Field(default="manual", description="格式：string enum。固定值：`manual`=人工正常结束；`cancel`=人工取消。`action=finish` 时传 `manual`；`action=cancel` 时建议传 `cancel`，如果省略也会按取消处理。`auto_timeout` 是边缘服务内部达到预计时长自动停止时使用，不是服务端入参。")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "taskId": "record_20260605_001",
                    "action": "finish",
                    "operatorUserId": "u_001",
                    "stopReason": "manual",
                },
                {
                    "taskId": "record_20260605_001",
                    "action": "cancel",
                    "operatorUserId": "u_001",
                    "stopReason": "cancel",
                },
            ]
        }
    }


class MobileRecordExtendRequest(BaseModel):
    taskId: str = Field(..., description="格式：string。要延长时长的移动录制任务ID，必须与 start 接口传入的 taskId 一致。示例：`record_20260605_001`。")
    extendSeconds: int = Field(default=1800, ge=60, description="格式：int，单位：秒。本次额外延长的录制时长，最小 `60`，当前边缘服务单次最多延长 3 小时。例如 `1800`=再延长30分钟。")
    operatorUserId: str = Field(default="", description="格式：string。发起延长操作的人ID，可为空字符串。默认认为是当前录制人本人延长；如果服务端代操作，可传实际操作人ID用于日志追踪。")

    model_config = {
        "json_schema_extra": {
            "example": {
                "taskId": "record_20260605_001",
                "extendSeconds": 1800,
                "operatorUserId": "u_001",
            }
        }
    }


def create_mobile_record_router(*, service) -> APIRouter:
    router = APIRouter()

    def _base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def _json(data: dict[str, Any] | list[Any] | None = None, *, code: str = "SUCCESS", message: str = "查询成功", status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            {
                "code": str(code or "SUCCESS"),
                "message": str(message or "查询成功"),
                "data": data if data is not None else {},
            },
            status_code=int(status_code),
        )

    def _start_json(code: str, message: str, data: dict[str, Any] | None = None, *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            {
                "code": str(code or "FAILED"),
                "message": str(message or "其他原因"),
                "data": data if data is not None else {},
            },
            status_code=int(status_code),
        )

    def _start_failed(message: str, *, status_code: int = 500) -> JSONResponse:
        return _start_json("FAILED", message, status_code=status_code)

    def _queue_start_failure_callback(payload: dict[str, Any], message: str) -> None:
        with_payload = dict(payload or {})
        asyncio.create_task(service.send_callback_for_start_failure(with_payload, message))

    def _service_start_failure_message(failure_code: str, failure_message: str) -> str:
        code = str(failure_code or "").strip()
        message = str(failure_message or "").strip()
        if _is_disk_full_error(message):
            return "磁盘已满或剩余空间不足"
        if _is_network_error(message):
            return "录像设备取流失败"
        if _is_service_start_error(message):
            return "录制进程或依赖服务启动失败"
        if code == "TASK_EXISTS":
            return "任务ID重复"
        if code == "CLASSROOM_RECORDING_CONFLICT":
            return "教室已被占用"
        if code == "CHANNEL_RECORDING_CONFLICT":
            return "当前通道/教室已有录制任务"
        if code == "USER_RECORDING_CONFLICT":
            return "录制人有进行中的录制任务"
        if code == "BAD_REQUEST":
            return "录制参数错误"
        return message or "其他原因"

    def _stop_message(action: str, ok: bool, data: dict[str, Any]) -> str:
        if ok:
            return "取消录制成功" if action == "cancel" else "停止录制成功"
        code = str(data.get("code") or "").strip()
        message = str(data.get("message") or "").strip()
        if code == "NOT_FOUND":
            return "录制任务不存在"
        if code == "BAD_REQUEST":
            return "参数错误"
        if str(data.get("status") or "") == "failed":
            return str(data.get("errorMessage") or "停止后文件不可用")
        return message or "停止录制失败"

    def _extend_message(ok: bool, data: dict[str, Any]) -> str:
        if ok:
            return "延长录制成功"
        code = str(data.get("code") or "").strip()
        message = str(data.get("message") or "").strip()
        if code == "NOT_FOUND":
            return "录制任务不存在"
        if code == "BAD_STATUS":
            return "任务不是录制中，不能延长"
        return message or "延长录制失败"

    def _is_disk_full_error(exc: BaseException | str) -> bool:
        text = str(exc or "").lower()
        return any(token in text for token in ("no space left", "errno 28", "disk full", "磁盘已满", "空间不足", "not enough space"))

    def _is_network_error(exc: BaseException | str) -> bool:
        text = str(exc or "").lower()
        return any(
            token in text
            for token in (
                "实时流探测超时",
                "实时流探测失败",
                "实时流不可播放",
                "实时流首包探测超时",
                "实时流首包探测失败",
                "实时流无法输出首包",
                "timed out",
                "timeout",
                "network",
                "connect",
                "refused",
                "unreachable",
                "网络",
                "连接",
            )
        )

    def _is_service_start_error(exc: BaseException | str) -> bool:
        text = str(exc or "").lower()
        return any(token in text for token in ("record_ffmpeg_start_failed", "ffmpeg", "popen", "createprocess", "启动失败"))

    def _classroom_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "classroomId": str(item.get("classroomId") or ""),
            "selectable": bool(item.get("selectable", True)),
            "taskId": str(item.get("taskId") or ""),
            "recordUserId": str(item.get("recordUserId") or ""),
            "recordUserName": str(item.get("recordUserName") or ""),
        }

    def _task_status_data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "taskId": str(item.get("taskId") or ""),
            "status": str(item.get("status") or ""),
            "classroomId": str(item.get("classroomId") or ""),
            "recordUserId": str(item.get("recordUserId") or ""),
            "recordUserName": str(item.get("recordUserName") or ""),
            "startTime": str(item.get("startTime") or ""),
            "finishTime": str(item.get("finishTime") or ""),
            "elapsedSeconds": int(item.get("elapsedSeconds") or 0),
            "remainingSeconds": int(item.get("remainingSeconds") or 0),
            "playUrl": str(item.get("playUrl") or ""),
            "segmentCount": int(item.get("segmentCount") or 0),
            "fileSize": int(item.get("fileSize") or 0),
            "durationSeconds": float(item.get("durationSeconds") or 0.0),
            "codec": str(item.get("codec") or ""),
            "errorMessage": str(item.get("errorMessage") or ""),
        }

    def _start_data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "taskId": str(item.get("taskId") or ""),
            "status": str(item.get("status") or ""),
            "recordUserId": str(item.get("recordUserId") or ""),
            "estimatedDurationSeconds": int(item.get("estimatedDurationSeconds") or 0),
            "elapsedSeconds": int(item.get("elapsedSeconds") or 0),
            "remainingSeconds": int(item.get("remainingSeconds") or 0),
        }

    def _stop_data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "taskId": str(item.get("taskId") or ""),
            "status": str(item.get("status") or ""),
            "playUrl": str(item.get("playUrl") or ""),
            "segmentCount": int(item.get("segmentCount") or 0),
            "fileSize": int(item.get("fileSize") or 0),
            "durationSeconds": float(item.get("durationSeconds") or 0.0),
            "codec": str(item.get("codec") or ""),
            "errorMessage": str(item.get("errorMessage") or ""),
        }

    def _extend_data(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "taskId": str(item.get("taskId") or ""),
            "status": str(item.get("status") or ""),
            "extendDurationSeconds": int(item.get("extendDurationSeconds") or 0),
            "remainingSeconds": int(item.get("remainingSeconds") or 0),
            "maxEndTime": str(item.get("maxEndTime") or ""),
        }

    @router.get(
        "/api/mobile-record/classrooms/status",
        summary="查询教室录制占用状态",
        response_model=MobileRecordClassroomStatusResponse,
        description=(
            "给选择教室页面使用。传入逗号分隔的 classroomIds 后，返回每个教室是否可选择。"
            "如果不可选择，同时返回当前占用任务和录制人。\n\n"
            "字段解释：\n"
            "- `classroomIds`：要查询的教室ID列表，使用英文逗号分隔。\n"
            "- `selectable`：是否允许选择该教室并发起录制，前端可录制判断只看这个字段。\n"
            "- `taskId`、`recordUserId`、`recordUserName`：selectable=false 时返回当前占用信息；selectable=true 时为空。\n\n"
            "返回 code：\n"
            "- `SUCCESS`：查询成功。\n"
            "- `FAILED`：查询失败。\n\n"
            "message 场景：\n"
            "- `查询成功`：已成功返回教室占用状态。\n"
            "- `查询教室录制占用状态失败：具体原因`：查询过程中发生异常。"
        ),
    )
    async def api_mobile_record_classrooms_status(
        classroomIds: str = Query(default="", description="格式：string。逗号分隔的教室ID列表，例如 `room_301,room_302`。用于批量刷新这些教室当前是否被占用；为空时返回当前所有录制中的教室。"),
    ):
        try:
            ids = [item.strip() for item in str(classroomIds or "").split(",") if item.strip()]
            return _json({"items": [_classroom_item(item) for item in service.classroom_statuses(ids)]}, code="SUCCESS", message="查询成功")
        except Exception as exc:
            return _json({"items": []}, code="FAILED", message=f"查询教室录制占用状态失败：{exc}", status_code=500)

    @router.post(
        "/api/mobile-record/start",
        summary="开始移动录制",
        response_model=MobileRecordStartResponse,
        description=(
            "服务端下发开始录制指令。NVR 连接字段与 `/api/stream/open` 保持一致；区别是本接口会启动独立 ffmpeg 进程录制 HLS，"
            "而不是返回实时预览流。边缘服务会在点击开始这一刻再次校验教室和录制人是否已被占用。\n\n"
            "业务约束：\n"
            "- 同一教室同一时刻只能有一个进行中的录制任务。\n"
            "- 同一录制人同一时刻只能有一个进行中的录制任务。\n"
            "- `estimatedDurationSeconds` 单位是秒，到时后边缘服务自动结束录制。\n"
            "- `callbackUrl` 字段已废弃，当前版本会忽略该入参；边缘服务统一使用本机配置中的 `serverAddress` 自动拼接默认回调地址：`{serverAddress}/api/v1/record-task/callback`。\n"
            "- 输出格式固定为 HLS，`playUrl` 在完成后可直接给 H5 播放。\n\n"
            "自动结束与回调：\n"
            "- start 成功后，边缘服务会记录 `maxEndTime = startTime + estimatedDurationSeconds`。\n"
            "- 边缘服务内部有调度器约每 5 秒检查一次到期任务；如果到达预计录制时间仍未手动结束，会自动按 `finish` 结束录制。\n"
            "- 自动结束时 `stopReason=auto_timeout`，该值只会出现在边缘服务回调服务端的结果里，不需要也不允许服务端调用 stop 时传入。\n"
            "- 只要本机已配置 `serverAddress`，开始成功、启动失败、自动结束、人工结束、取消、失败或中断后，边缘服务都会主动 POST 到默认回调地址，服务端不需要再次调用 stop。\n\n"
            "callbackUrl 回调 payload 字段格式：\n"
            "- `taskId`：string，录制任务ID。\n"
            "- `classroomId`：string，教室ID。\n"
            "- `cameraId`：string，优先使用 nvrChannelId；没有时使用 nvrChannelNum。\n"
            "- `status`：string enum，可能值：`recording`、`finished`、`cancelled`、`failed`、`interrupted`。\n"
            "- `stopReason`：string 或 null，可能值：`manual`、`cancel`、`auto_timeout`。\n"
            "- `playUrl`：string 或 null，完成后 HLS 播放地址。\n"
            "- `outputDir`：string，本地录制目录。\n"
            "- `m3u8Path`：string，本地 index.m3u8 路径。\n"
            "- `segmentCount`：int，HLS 分片数量。\n"
            "- `fileSize`：int，分片总大小，单位字节。\n"
            "- `duration`：float，最终可播放时长，单位秒。\n"
            "- `codec`：string，常见值 `h264`、`hevc`，无法探测时为空字符串。\n"
            "- `format`：string，固定为 `hls`。\n"
            "- `startTime`、`finishTime`：string，格式 `yyyy-MM-dd HH:mm:ss`。\n"
            "- `errorMessage`：string 或 null，失败原因；无错误时为 null。\n\n"
            "回调重试：\n"
            "- 回调失败时边缘服务会自动重试，当前重试间隔约为 0秒、10秒、30秒、60秒、300秒。\n\n"
            "启动结果 code：\n"
            "- `SUCCESS`：启动录制成功，message 固定为“启动录制成功”。\n"
            "- `FAILED`：启动录制失败。\n\n"
            "message 场景：\n"
            "- `启动录制成功`：录制任务已创建，边缘服务已开始录制。\n"
            "- `磁盘已满或剩余空间不足`：录制目录所在磁盘空间不足，无法继续写入 HLS 文件。\n"
            "- `录像设备取流失败`：录像设备无法建立实时流、RTSP 地址不可用、网络超时或无法输出首包。\n"
            "- `接口服务未启动`：边缘服务接口不可访问。说明：如果请求已经到达本接口，通常不会由本接口返回该提示。\n"
            "- `录制进程或依赖服务启动失败：具体原因`：ffmpeg 或录制依赖启动失败。\n"
            "- `任务ID重复`：taskId 已存在，不能重复启动同一个录制任务。\n"
            "- `教室已被占用`：classroomId 对应教室已有进行中的录制任务。\n"
            "- `当前通道/教室已有录制任务`：同一个 NVR 设备和通道已有进行中的录制任务，用于防止前端传错 classroomId 时同一物理教室被重复录制。\n"
            "- `录制人有进行中的录制任务`：recordUserId 已有另一个进行中的录制任务。\n"
            "- `录制参数错误：具体原因`：必要参数缺失或参数不合法。\n"
            "- `其他原因：具体原因`：未归类的启动失败。"
        ),
    )
    async def api_mobile_record_start(req: MobileRecordStartRequest, request: Request):
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        try:
            client_ip = request.client.host if request.client else ""
            payload["__clientIp"] = str(client_ip or "")
            _log.info(
                "START client_ip=%s taskId=%s campusCode=%s classroomId=%s nvrDeviceId=%s nvrIp=%s nvrPort=%s "
                "nvrAccount=%s nvrPassword=%s nvrChannelId=%s nvrChannelNum=%s recordUserId=%s recordUserName=%s estimatedDurationSeconds=%s callbackUrl(deprecated)=%s",
                client_ip,
                str(payload.get("taskId") or ""),
                str(payload.get("campusCode") or ""),
                str(payload.get("classroomId") or ""),
                payload.get("nvrDeviceId"),
                str(payload.get("ipAddress") or ""),
                payload.get("port"),
                str(payload.get("account") or ""),
                _mask_secret(payload.get("password")),
                str(payload.get("nvrChannelId") or ""),
                payload.get("nvrChannelNum"),
                str(payload.get("recordUserId") or ""),
                str(payload.get("recordUserName") or ""),
                payload.get("estimatedDurationSeconds"),
                str(payload.get("callbackUrl") or ""),
            )
            ok, data, status = await service.start_recording(payload, request_base_url=_base_url(request))
        except ValueError as exc:
            _queue_start_failure_callback(payload, f"录制参数错误：{exc}")
            return _start_failed(f"录制参数错误：{exc}", status_code=400)
        except OSError as exc:
            if _is_disk_full_error(exc):
                _queue_start_failure_callback(payload, "磁盘已满或剩余空间不足")
                return _start_failed("磁盘已满或剩余空间不足", status_code=507)
            _queue_start_failure_callback(payload, f"录制进程或依赖服务启动失败：{exc}")
            return _start_failed(f"录制进程或依赖服务启动失败：{exc}", status_code=500)
        except Exception as exc:
            if _is_disk_full_error(exc):
                _queue_start_failure_callback(payload, "磁盘已满或剩余空间不足")
                return _start_failed("磁盘已满或剩余空间不足", status_code=507)
            if _is_service_start_error(exc):
                _queue_start_failure_callback(payload, f"录制进程或依赖服务启动失败：{exc}")
                return _start_failed(f"录制进程或依赖服务启动失败：{exc}", status_code=500)
            if _is_network_error(exc):
                _queue_start_failure_callback(payload, "录像设备取流失败")
                return _start_failed("录像设备取流失败", status_code=502)
            _queue_start_failure_callback(payload, f"其他原因：{exc}")
            return _start_failed(f"其他原因：{exc}", status_code=500)
        if ok:
            return _start_json("SUCCESS", "启动录制成功", _start_data(data), status_code=status)
        failure_code = str(data.get("code") or "").strip()
        failure_message = str(data.get("message") or "").strip()
        _queue_start_failure_callback(payload, _service_start_failure_message(failure_code, failure_message))
        return _start_failed(_service_start_failure_message(failure_code, failure_message), status_code=status)

    @router.post(
        "/api/mobile-record/stop",
        summary="结束或取消移动录制",
        response_model=MobileRecordStopResponse,
        description=(
            "停止录制任务。action=finish 表示结束并保留HLS文件，action=cancel 表示取消录制并删除本地文件。"
            "接口幂等，已结束任务重复调用会返回当前最终状态。\n\n"
            "字段解释：\n"
            "- `taskId`：格式 string，要停止的移动录制任务ID。\n"
            "- `action`：格式 string enum，`finish`=正常结束录制，保留文件，生成可播放地址；`cancel`=取消录制，删除本地录制目录。\n"
            "- `operatorUserId`：格式 string，可为空，用于记录是谁发起停止或取消。\n"
            "- `stopReason`：格式 string enum，人工调用 finish 时通常为 `manual`；人工调用 cancel 时通常为 `cancel`。`auto_timeout` 只用于边缘服务内部自动停止，不允许服务端手动传。\n\n"
            "返回 code：\n"
            "- `SUCCESS`：停止/取消成功，或任务已经处于最终状态。\n"
            "- `FAILED`：停止/取消失败。\n\n"
            "message 场景：\n"
            "- `停止录制成功`：action=finish，录制已结束并保留本地 HLS 文件。\n"
            "- `取消录制成功`：action=cancel，录制已取消并删除本地文件。\n"
            "- `参数错误：action 仅支持 finish 或 cancel`：action 不是允许值。\n"
            "- `参数错误：auto_timeout 仅用于边缘服务内部自动停止，服务端接口不要手动传`：服务端错误传入内部原因。\n"
            "- `参数错误：action=finish 时 stopReason 不能传 cancel`：停止方式和原因冲突。\n"
            "- `录制任务不存在`：taskId 不存在。\n"
            "- `停止后文件不可用`：正常结束后 HLS 文件未生成或不可用。\n"
            "- `停止录制失败：具体原因`：其他停止失败原因。"
        ),
    )
    async def api_mobile_record_stop(req: MobileRecordStopRequest, request: Request):
        action = str(req.action or "finish").strip().lower()
        if action not in {"finish", "cancel"}:
            return _json(code="FAILED", message="参数错误：action 仅支持 finish 或 cancel", status_code=400)
        requested_reason = str(req.stopReason or "").strip().lower()
        if requested_reason == "auto_timeout":
            return _json(code="FAILED", message="参数错误：auto_timeout 仅用于边缘服务内部自动停止，服务端接口不要手动传", status_code=400)
        if action == "finish" and requested_reason == "cancel":
            return _json(code="FAILED", message="参数错误：action=finish 时 stopReason 不能传 cancel", status_code=400)
        reason = requested_reason or ("cancel" if action == "cancel" else "manual")
        if action == "cancel":
            reason = "cancel"
        try:
            ok, data, status = await service.stop_recording(
                req.taskId,
                action=action,
                operator_user_id=req.operatorUserId,
                reason=reason,
                request_base_url=_base_url(request),
            )
        except Exception as exc:
            return _json(_stop_data({}), code="FAILED", message=f"停止录制失败：{exc}", status_code=500)
        return _json(_stop_data(data), code=("SUCCESS" if ok else "FAILED"), message=_stop_message(action, ok, data), status_code=status)

    @router.post(
        "/api/mobile-record/extend",
        summary="延长移动录制时长",
        response_model=MobileRecordExtendResponse,
        description=(
            "录制中可调用。更新自动停止时间 maxEndTime，后台自动停止调度会按最新时间生效。\n\n"
            "字段解释：\n"
            "- `taskId`：格式 string，要延长的移动录制任务ID。\n"
            "- `extendSeconds`：格式 int，本次额外延长的秒数，单位是秒。例如 `1800` 表示再延长 30 分钟。\n"
            "- `operatorUserId`：格式 string，可选字段，默认认为当前录制人本人延长。\n\n"
            "返回 code：\n"
            "- `SUCCESS`：延长成功。\n"
            "- `FAILED`：延长失败。\n\n"
            "message 场景：\n"
            "- `延长录制成功`：录制任务仍在进行中，预计自动停止时间已延后。\n"
            "- `录制任务不存在`：taskId 不存在。\n"
            "- `任务不是录制中，不能延长`：任务已完成、已取消、失败、中断或正在停止，不能继续延长。\n"
            "- `延长录制失败：具体原因`：其他延长失败原因。"
        ),
    )
    async def api_mobile_record_extend(req: MobileRecordExtendRequest):
        try:
            ok, data, status = await service.extend_recording(req.taskId, req.extendSeconds, req.operatorUserId)
        except Exception as exc:
            return _json(_extend_data({}), code="FAILED", message=f"延长录制失败：{exc}", status_code=500)
        return _json(_extend_data(data), code=("SUCCESS" if ok else "FAILED"), message=_extend_message(ok, data), status_code=status)

    @router.get(
        "/api/mobile-record/status",
        summary="查询移动录制任务状态",
        response_model=MobileRecordStatusResponse,
        description=(
            "录制中页面、完成页和服务端补偿查询使用。支持按 taskId 查询单个任务，也支持按 classroomId 查询该教室当前进行中的任务。"
            "任务可能提前结束、取消、失败或中断，具体结果看返回的 `status` 字段，不要只依赖预计结束时间。\n\n"
            "返回字段说明：\n"
            "- `status`：任务状态，可能是 `recording`、`finished`、`cancelled`、`failed`、`interrupted` 等。\n"
            "- `elapsedSeconds`：已经录制了多少秒，单位：秒。\n"
            "- `remainingSeconds`：距离自动停止还剩多少秒，单位：秒。\n"
            "- `playUrl`：录制完成后，H5 直接访问的 HLS 播放地址；未完成或失败时为空字符串。\n"
            "- `segmentCount`：当前已经生成了多少个 `.ts` 分片。\n"
            "- `codec`：当前成片的主视频编码，常见值 `h264`、`hevc`，无法探测时为空字符串。\n\n"
            "返回 code：\n"
            "- `SUCCESS`：查询成功。\n"
            "- `FAILED`：查询失败。\n\n"
            "message 场景：\n"
            "- `查询成功`：已返回任务状态。任务不存在时也会正常返回空状态数据，code 仍为 SUCCESS。\n"
            "- `查询移动录制任务状态失败：具体原因`：查询过程中发生异常。"
        ),
    )
    async def api_mobile_record_status(
        request: Request,
        taskId: str = Query(default="", description="格式：string。录制任务ID。传这个时，查询这个任务的详细状态。示例：`record_20260605_001`。"),
        classroomId: str = Query(default="", description="格式：string。教室ID。传这个时，查询这个教室当前是否有进行中的录制。示例：`room_301`。"),
    ):
        try:
            data = service.get_status(task_id=taskId, classroom_id=classroomId, request_base_url=_base_url(request))
            return _json(_task_status_data(data), code="SUCCESS", message="查询成功")
        except Exception as exc:
            return _json(_task_status_data({}), code="FAILED", message=f"查询移动录制任务状态失败：{exc}", status_code=500)

    @router.get(
        "/api/mobile-record/list",
        summary="查询本地移动录制任务列表",
        response_model=MobileRecordListResponse,
        description=(
            "用于服务端补偿查询、排障或后续管理页面。支持按教室、录制人、状态、日期过滤。\n\n"
            "查询参数说明：\n"
            "- `classroomId`：格式 string，按教室过滤，可为空。\n"
            "- `recordUserId`：格式 string，按录制人过滤，可为空。\n"
            "- `status`：格式 string enum，按任务状态过滤，可为空；例如 `recording`、`finished`、`failed`。\n"
            "- `date`：格式 string，按开始日期过滤，可为空；格式 `YYYY-MM-DD`。\n"
            "- `limit`：格式 int，最多返回多少条，默认 `100`。\n\n"
            f"任务状态固定值：{TASK_STATUS_DESC}\n\n"
            "返回 code：\n"
            "- `SUCCESS`：查询成功。\n"
            "- `FAILED`：查询失败。\n\n"
            "message 场景：\n"
            "- `查询成功`：已返回符合条件的任务列表。没有匹配任务时 items 为空数组，code 仍为 SUCCESS。\n"
            "- `查询移动录制任务列表失败：具体原因`：查询过程中发生异常。"
        ),
    )
    async def api_mobile_record_list(
        classroomId: str = Query(default="", description="格式：string。按教室ID过滤，可为空。示例：`room_301`。"),
        recordUserId: str = Query(default="", description="格式：string。按录制人ID过滤，可为空。示例：`u_001`。"),
        status: str = Query(default="", description="格式：string enum。按任务状态过滤，可为空；例如 `recording`、`finished`、`cancelled`、`failed`、`interrupted`。"),
        date: str = Query(default="", description="格式：string。按开始日期过滤，可为空；格式 `YYYY-MM-DD`，例如 `2026-06-11`。"),
        limit: int = Query(default=100, description="格式：int。最多返回多少条记录，默认 `100`。"),
    ):
        try:
            items = service.list_tasks(classroom_id=classroomId, record_user_id=recordUserId, status=status, date=date, limit=limit)
            return _json({"items": [_task_status_data(item) for item in items]}, code="SUCCESS", message="查询成功")
        except Exception as exc:
            return _json({"items": []}, code="FAILED", message=f"查询移动录制任务列表失败：{exc}", status_code=500)

    @router.get(
        "/api/mobile-record/play/{task_id}/{path:path}",
        summary="播放移动录制HLS文件",
        description=(
            "H5拿到playUrl后直接访问。仅 finished 状态任务允许读取 index.m3u8 和 segment_*.ts。\n\n"
            "路径说明：\n"
            "- `task_id`：格式 string，录制任务ID。\n"
            "- `path`：格式 string，录制目录下的相对文件路径，通常是 `index.m3u8`，也可能是某个 `.ts` 分片。\n\n"
            "返回说明：\n"
            "- 成功：直接返回 HLS 文件流，`index.m3u8` 的媒体类型为 `application/vnd.apple.mpegurl`，`.ts` 分片的媒体类型为 `video/mp2t`。\n"
            "- JSON 错误 code：`FAILED`=任务未完成、文件不存在或路径不合法。\n\n"
            "message 场景：\n"
            "- 成功时不返回 JSON，直接返回文件流。\n"
            "- `录制文件不存在或尚未生成`：任务未完成、文件不存在或路径不合法。"
        ),
    )
    async def api_mobile_record_play(task_id: str, path: str):
        file_path = service.resolve_play_file(task_id, path or "index.m3u8")
        if file_path is None:
            return _json(code="FAILED", message="录制文件不存在或尚未生成", status_code=404)
        suffix = file_path.suffix.lower()
        media_type = "application/vnd.apple.mpegurl" if suffix == ".m3u8" else ("video/mp2t" if suffix == ".ts" else "application/octet-stream")
        return FileResponse(str(file_path), media_type=media_type)

    return router
