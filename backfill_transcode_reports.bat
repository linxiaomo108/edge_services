@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "EDGE_BASE_URL=%~1"
if "%EDGE_BASE_URL%"=="" set "EDGE_BASE_URL=http://127.0.0.1:18080"
set "PS_FILE=%TEMP%\edge_backfill_transcode_%RANDOM%%RANDOM%.ps1"
(
echo $ErrorActionPreference = 'Stop'
echo $baseUrl = '%EDGE_BASE_URL%'
echo $payload = @^
echo {
echo   targets = @(
echo     @{ lessonId = '2019320118957699078'; taskTypes = @('student') },
echo     @{ lessonId = '2027325456193290246'; taskTypes = @('student','teacher') },
echo     @{ lessonId = '2027272767406415878'; taskTypes = @('student') }
echo   )
echo }
echo $json = $payload ^| ConvertTo-Json -Depth 6
echo Write-Host "POST $baseUrl/api/report/backfill-transcode" -ForegroundColor Cyan
echo Write-Host $json -ForegroundColor DarkGray
echo $resp = Invoke-RestMethod -Method Post -Uri ($baseUrl + '/api/report/backfill-transcode') -ContentType 'application/json; charset=utf-8' -Body $json -TimeoutSec 600
echo $respJson = $resp ^| ConvertTo-Json -Depth 8
echo Write-Host ''
echo Write-Host '补报结果：' -ForegroundColor Green
echo Write-Host $respJson
echo if (-not $resp.ok) { exit 2 }
echo if ($resp.failed -gt 0) { exit 3 }
) > "%PS_FILE%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
del "%PS_FILE%" >nul 2>nul
if not "%EXIT_CODE%"=="0" (
  echo.
  echo 补报执行失败，退出码=%EXIT_CODE%
  exit /b %EXIT_CODE%
)
echo.
echo 补报执行完成。
exit /b 0
