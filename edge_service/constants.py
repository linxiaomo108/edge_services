from __future__ import annotations

from enum import IntEnum


class TaskStatus(IntEnum):
    PENDING = 0
    RUNNING = 1
    SUCCESS = 2
    FAILED = 3
    PAUSED = 4


class StepStatus(IntEnum):
    PENDING = 0
    RUNNING = 1
    SUCCESS = 2
    FAILED = 3
    PAUSED = 4


class StepCode(str):
    DOWNLOAD = "DOWNLOAD"
    TRANSCODE = "TRANSCODE"
    SPEECH = "SPEECH"
    SUBTITLE = "SUBTITLE"
    ANALYSIS = "ANALYSIS"


class TaskKind(str):
    CAMERA = "CameraTask"
    COURSE = "CourseTask"
