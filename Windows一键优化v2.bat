@echo off
chcp 65001 >nul
title Windows 一键优化脚本 v2
color 0A
setlocal

::==============================
:: 0. 管理员权限检查与运行选项
::==============================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键选择 "以管理员身份运行"！
    pause
    exit /b 1
)

echo ============================================
echo         Windows 一键优化脚本 v2
echo   注意：本脚本将完全禁用 Windows 更新
echo ============================================
echo.

set "POWER_CHOICE=1"
echo [选项1] 电源模式：
echo   [1] 高性能（推荐台式机）
echo   [2] 平衡   （推荐笔记本，兼顾续航）
set /p POWER_CHOICE=请输入 1 或 2，直接回车默认 1：
if not "%POWER_CHOICE%"=="1" if not "%POWER_CHOICE%"=="2" set "POWER_CHOICE=1"
echo.

set "RESET_BASE=N"
set /p RESET_BASE=是否执行 DISM /ResetBase（更省空间，但已装更新将无法卸载）[y/N]：
if /i "%RESET_BASE%"=="y" (set "RESET_BASE=Y") else (set "RESET_BASE=N")
echo.

set "HIBER=N"
set /p HIBER=是否关闭休眠以释放 hiberfil.sys 空间（会禁用休眠/快速启动）[y/N]：
if /i "%HIBER%"=="y" (set "HIBER=Y") else (set "HIBER=N")
echo.

set "CLEAN_CHAT=N"
set /p CLEAN_CHAT=是否清理微信图片缓存（不删聊天记录，图片重新联网时可再下载）[y/N]：
if /i "%CLEAN_CHAT%"=="y" (set "CLEAN_CHAT=Y") else (set "CLEAN_CHAT=N")
echo.

::==============================
:: 1. 创建系统还原点（安全第一）
::==============================
echo [1/18] 正在创建系统还原点...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Checkpoint-Computer -Description '优化脚本前自动还原点' -RestorePointType MODIFY_SETTINGS -ErrorAction Stop; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% equ 0 (
    echo       [成功] 系统还原点创建成功，出问题可随时还原。
) else (
    echo       [跳过] 还原点创建失败（可能未开启还原点功能，或 24 小时内已创建过）。
)
echo.

::==============================
:: 2. 清理用户与系统临时文件
::==============================
echo [2/18] 正在清理临时文件...
del /f /s /q "%TEMP%\*" >nul 2>&1
for /d %%i in ("%TEMP%\*") do rd /s /q "%%i" >nul 2>&1
del /f /s /q "%SystemRoot%\Temp\*" >nul 2>&1
for /d %%i in ("%SystemRoot%\Temp\*") do rd /s /q "%%i" >nul 2>&1
for /d %%U in ("%SystemDrive%\Users\*") do del /f /s /q "%%~U\AppData\Local\Temp\*" >nul 2>&1
echo       [成功] 临时文件清理完成（Prefetch 保留，删除反而拖慢程序启动）。
echo.

::==============================
:: 3. 深度清理 Windows 更新垃圾
::==============================
echo [3/18] 正在深度清理 Windows 更新垃圾...
net stop wuauserv /y >nul 2>&1
net stop bits /y >nul 2>&1
net stop UsoSvc /y >nul 2>&1
net stop DoSvc /y >nul 2>&1
del /f /s /q "%SystemRoot%\SoftwareDistribution\Download\*" >nul 2>&1
del /f /s /q "%SystemRoot%\SoftwareDistribution\DataStore\*" >nul 2>&1
del /f /s /q "%SystemRoot%\SoftwareDistribution\DeliveryOptimization\*" >nul 2>&1
del /f /s /q "%SystemRoot%\SoftwareDistribution\SelfUpdate\*" >nul 2>&1
del /f /s /q "%SystemRoot%\SoftwareDistribution\Logs\*" >nul 2>&1
del /f /q "%SystemRoot%\SoftwareDistribution\WindowsUpdate.log" >nul 2>&1
del /f /q "%SystemRoot%\WindowsUpdate.log" >nul 2>&1
del /f /s /q "%SystemRoot%\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*" >nul 2>&1
del /f /s /q "%SystemRoot%\ServiceProfiles\LocalService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*" >nul 2>&1
del /f /s /q "%ProgramData%\Microsoft\Windows\DeliveryOptimization\*" >nul 2>&1
echo       [成功] 更新缓存、传递优化数据库已清理（更新服务将在第 11 步禁用）。
echo.

::==============================
:: 4. 清理系统升级残留（Windows.old 等）
::==============================
echo [4/18] 正在清理系统升级残留...
if exist "%SystemDrive%\Windows.old" (
    takeown /f "%SystemDrive%\Windows.old" /r /d y >nul 2>&1
    icacls "%SystemDrive%\Windows.old" /grant *S-1-5-32-544:F /t /c /q >nul 2>&1
    rd /s /q "%SystemDrive%\Windows.old" >nul 2>&1
)
if exist "%SystemDrive%\$Windows.~BT" (
    takeown /f "%SystemDrive%\$Windows.~BT" /r /d y >nul 2>&1
    icacls "%SystemDrive%\$Windows.~BT" /grant *S-1-5-32-544:F /t /c /q >nul 2>&1
    rd /s /q "%SystemDrive%\$Windows.~BT" >nul 2>&1
)
if exist "%SystemDrive%\$Windows.~WS" (
    takeown /f "%SystemDrive%\$Windows.~WS" /r /d y >nul 2>&1
    icacls "%SystemDrive%\$Windows.~WS" /grant *S-1-5-32-544:F /t /c /q >nul 2>&1
    rd /s /q "%SystemDrive%\$Windows.~WS" >nul 2>&1
)
del /f /s /q "%SystemRoot%\Panther\*" >nul 2>&1
del /f /s /q "%SystemRoot%\Logs\MoSetup\*" >nul 2>&1
if exist "%SystemDrive%\Windows.old" (
    echo       [部分] 仍有残留，将在第 16 步磁盘清理中继续移除。
) else (
    echo       [成功] 升级残留（Windows.old / ~BT / ~WS）已清理。
)
echo.

::==============================
:: 5. 组件存储清理（WinSxS 旧组件）
::==============================
echo [5/18] 正在清理系统组件存储（可能需要几分钟）...
DISM /Online /Cleanup-Image /StartComponentCleanup >nul 2>&1
if "%RESET_BASE%"=="Y" (
    DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase >nul 2>&1
    echo       [完成] 已执行 /ResetBase，已装更新将无法卸载。
) else (
    echo       [成功] 旧组件已清理（保留更新卸载能力）。
)
echo.

::==============================
:: 6. 清理回收站、缩略图与图标缓存（全部用户）
::==============================
echo [6/18] 正在清空回收站与缩略图/图标缓存...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue" >nul 2>&1
for /d %%U in ("%SystemDrive%\Users\*") do (
    del /f /q "%%~U\AppData\Local\Microsoft\Windows\Explorer\thumbcache_*.db" >nul 2>&1
    del /f /q "%%~U\AppData\Local\Microsoft\Windows\Explorer\iconcache_*.db" >nul 2>&1
)
ie4uinit.exe -show >nul 2>&1
echo       [成功] 回收站已清空，全部用户缩略图/图标缓存已重建。
echo.

::==============================
:: 7. 清理崩溃报告、系统日志与显卡缓存
::==============================
echo [7/18] 正在清理崩溃报告、日志与显卡缓存...
del /f /s /q "%SystemRoot%\Minidump\*" >nul 2>&1
del /f /q "%SystemRoot%\MEMORY.DMP" >nul 2>&1
del /f /s /q "%SystemRoot%\LiveKernelReports\*" >nul 2>&1
del /f /s /q "%ProgramData%\Microsoft\Windows\WER\ReportArchive\*" >nul 2>&1
del /f /s /q "%ProgramData%\Microsoft\Windows\WER\ReportQueue\*" >nul 2>&1
del /f /s /q "%ProgramData%\Microsoft\Windows\WER\Temp\*" >nul 2>&1
for /d %%U in ("%SystemDrive%\Users\*") do (
    del /f /s /q "%%~U\AppData\Local\Microsoft\Windows\WER\ReportArchive\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Microsoft\Windows\WER\ReportQueue\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\CrashDumps\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\NVIDIA\DXCache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\AMD\DxCache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Intel\ShaderCache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\D3DSCache\*" >nul 2>&1
)
del /f /s /q "%SystemRoot%\Logs\DISM\*" >nul 2>&1
del /f /s /q "%SystemRoot%\Logs\CBS\*.log" >nul 2>&1
del /f /s /q "%SystemRoot%\debug\*.log" >nul 2>&1
del /f /s /q "%SystemRoot%\debug\*.txt" >nul 2>&1
for /f "delims=" %%L in ('wevtutil el') do wevtutil cl "%%L" >nul 2>&1
echo       [成功] 崩溃报告、系统事件日志、显卡着色器缓存已清理。
echo.

::==============================
:: 8. 清理浏览器与软件缓存（全部用户）
::==============================
echo [8/18] 正在清理浏览器与软件缓存（请先关闭浏览器）...
for /d %%U in ("%SystemDrive%\Users\*") do (
    for /d %%P in ("%%~U\AppData\Local\Google\Chrome\User Data\*") do (
        del /f /s /q "%%~P\Cache\*" >nul 2>&1
        del /f /s /q "%%~P\Code Cache\*" >nul 2>&1
        del /f /s /q "%%~P\GPUCache\*" >nul 2>&1
    )
    for /d %%P in ("%%~U\AppData\Local\Microsoft\Edge\User Data\*") do (
        del /f /s /q "%%~P\Cache\*" >nul 2>&1
        del /f /s /q "%%~P\Code Cache\*" >nul 2>&1
        del /f /s /q "%%~P\GPUCache\*" >nul 2>&1
    )
    for /d %%P in ("%%~U\AppData\Local\BraveSoftware\Brave-Browser\User Data\*") do (
        del /f /s /q "%%~P\Cache\*" >nul 2>&1
        del /f /s /q "%%~P\Code Cache\*" >nul 2>&1
    )
    for /d %%P in ("%%~U\AppData\Roaming\Mozilla\Firefox\Profiles\*") do rd /s /q "%%~P\cache2" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Opera Software\Opera Stable\Cache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Opera Software\Opera GX Stable\Cache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Microsoft\Windows\INetCache\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Tencent\QQ\Temp\*" >nul 2>&1
    del /f /s /q "%%~U\AppData\Local\Temp\*_QQ\*" >nul 2>&1
)
if "%CLEAN_CHAT%"=="Y" (
    for /d %%U in ("%SystemDrive%\Users\*") do (
        for /d %%C in ("%%~U\Documents\WeChat Files\*") do rd /s /q "%%~C\FileStorage\Cache" >nul 2>&1
        for /d %%C in ("%%~U\OneDrive\Documents\WeChat Files\*") do rd /s /q "%%~C\FileStorage\Cache" >nul 2>&1
    )
    echo       [成功] 微信图片缓存已清理。
)
echo       [成功] 全部用户浏览器缓存已清理（不影响登录状态与收藏夹）。
echo.

::==============================
:: 9. 清理系统帮助与字体缓存
::==============================
echo [9/18] 正在清理系统辅助文件...
del /f /s /q "%SystemRoot%\Help\*" >nul 2>&1
del /f /s /q "%SystemRoot%\System32\catroot2\Temp\*" >nul 2>&1
net stop FontCache >nul 2>&1
del /f /q "%LOCALAPPDATA%\Microsoft\Windows\FontCache\*.dat" >nul 2>&1
del /f /q "%SystemRoot%\ServiceProfiles\LocalService\AppData\Local\FontCache\*.dat" >nul 2>&1
net start FontCache >nul 2>&1
echo       [成功] 帮助文件、字体缓存已清理（驱动仓库不清理，避免驱动损坏）。
echo.

::==============================
:: 10. 禁用遥测与隐私追踪（服务+注册表+任务）
::==============================
echo [10/18] 正在禁用遥测与隐私追踪...
sc config DiagTrack start= disabled >nul 2>&1
sc config dmwappushservice start= disabled >nul 2>&1
sc config diagsvc start= disabled >nul 2>&1
sc config PcaSvc start= disabled >nul 2>&1
net stop DiagTrack /y >nul 2>&1
net stop dmwappushservice /y >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection" /v AllowTelemetry /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\DataCollection" /v DoNotShowFeedbackNotifications /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\AppCompat" /v AITEnable /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\AppCompat" /v DisableInventory /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\AppCompat" /v DisableTaggedTelemetry /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v EnableActivityFeed /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v PublishUserActivities /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v UploadUserActivities /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Cloud Content" /v DisableWindowsConsumerFeatures /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Cloud Content" /v DisableSoftLanding /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Cloud Content" /v DisableTailoredExperiencesWithDiagnosticData /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search" /v AllowCortana /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Windows Search" /v DisableWebSearch /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo" /v Enabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Siuf\Rules" /v NumberOfSIUFInPeriod /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Siuf\Rules" /v PeriodInNanoSeconds /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced" /v Start_TrackProgs /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\Windows Error Reporting" /v Disabled /t REG_DWORD /d 1 /f >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Application Experience\ProgramDataUpdater" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Customer Experience Improvement Program\Consolidator" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Customer Experience Improvement Program\UsbCeip" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Customer Experience Improvement Program\KernelCeipTask" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticDataCollector" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\DiskDiagnostic\Microsoft-Windows-DiskDiagnosticResolver" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Power Efficiency Diagnostics\AnalyzeSystem" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Maintenance\WinSAT" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\Windows Error Reporting\QueueReporting" /disable >nul 2>&1
echo       [成功] 遥测服务已停用，19 项隐私策略已写入，诊断任务已禁用。
echo.

::==============================
:: 11. 完全禁用 Windows 更新
::==============================
echo [11/18] 正在完全禁用 Windows 更新...
reg add "HKLM\SYSTEM\CurrentControlSet\Services\WaaSMedicSvc" /v Start /t REG_DWORD /d 4 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\UsoSvc" /v Start /t REG_DWORD /d 4 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\wuauserv" /v Start /t REG_DWORD /d 4 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\DoSvc" /v Start /t REG_DWORD /d 4 /f >nul 2>&1
reg add "HKLM\SYSTEM\CurrentControlSet\Services\bits" /v Start /t REG_DWORD /d 4 /f >nul 2>&1
sc config wuauserv start= disabled >nul 2>&1
sc config UsoSvc start= disabled >nul 2>&1
sc config DoSvc start= disabled >nul 2>&1
sc config bits start= disabled >nul 2>&1
net stop wuauserv /y >nul 2>&1
net stop UsoSvc /y >nul 2>&1
net stop DoSvc /y >nul 2>&1
net stop bits /y >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoUpdate /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v DisableWindowsUpdateAccess /t REG_DWORD /d 1 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v DoNotConnectToWindowsUpdateInternetLocations /t REG_DWORD /d 1 /f >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\WindowsUpdate\Scheduled Start" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\WindowsUpdate\Automatic App Update" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\WindowsUpdate\sih" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\WindowsUpdate\sihboot" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\Schedule Scan" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\Schedule Scan Static Task" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\Maintenance Install" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\USO_UxBroker_Flush" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\Refresh Settings" /disable >nul 2>&1
schtasks /change /tn "\Microsoft\Windows\UpdateOrchestrator\Backup Scan" /disable >nul 2>&1
echo       [成功] Windows 更新服务、计划任务与访问策略已全部禁用。
echo       [提示] 如需恢复更新，将上述 5 个服务的 Start 改回 3（bits 改回 2），
echo             删除 HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate 项，
echo             并重新启用相关计划任务即可。
echo.

::==============================
:: 12. 网络全套修复与连通性验证
::==============================
echo [12/18] 正在执行网络全套修复...
ipconfig /flushdns >nul 2>&1
ipconfig /registerdns >nul 2>&1
nbtstat -R >nul 2>&1
netsh winsock reset >nul 2>&1
netsh int ip reset >nul 2>&1
netsh int ipv6 reset >nul 2>&1
netsh winhttp reset proxy >nul 2>&1
netsh int tcp set global autotuninglevel=normal >nul 2>&1
netsh int tcp set global rss=enabled >nul 2>&1
ipconfig /release >nul 2>&1
ipconfig /renew >nul 2>&1
sc config Dnscache start= delayed-auto >nul 2>&1
sc config Dhcp start= delayed-auto >nul 2>&1
sc config NlaSvc start= delayed-auto >nul 2>&1
sc config netprofm start= delayed-auto >nul 2>&1
sc config WlanSvc start= delayed-auto >nul 2>&1
sc config EapHost start= demand >nul 2>&1
sc config dot3svc start= demand >nul 2>&1
sc config WinHttpAutoProxySvc start= demand >nul 2>&1
sc start Dnscache >nul 2>&1
sc start Dhcp >nul 2>&1
sc start NlaSvc >nul 2>&1
sc start netprofm >nul 2>&1
sc start WlanSvc >nul 2>&1
echo       [完成] DNS/代理已重置，TCP/IP 与 Winsock 已重置，8 项网络服务已修复。
echo.
echo       ---- 连通性验证（需重启后网络重置才完全生效）----
nslookup www.baidu.com
ping -n 2 223.5.5.5
if errorlevel 1 (
    echo       [注意] ping 未通，请重启电脑后重新检查网络。
) else (
    echo       [成功] 外网连通性验证通过。
)
echo.

::==============================
:: 13. 电源计划
::==============================
echo [13/18] 正在设置电源模式...
if "%POWER_CHOICE%"=="1" (
    powercfg /setactive SCHEME_MIN >nul 2>&1
    echo       [成功] 已切换到 "高性能" 电源模式。
) else (
    powercfg /setactive SCHEME_BALANCED >nul 2>&1
    echo       [成功] 已切换到 "平衡" 电源模式。
)
echo.

::==============================
:: 14. 视觉效果调整为最佳性能
::==============================
echo [14/18] 正在优化系统视觉效果...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" /v VisualFXSetting /t REG_DWORD /d 3 /f >nul 2>&1
reg add "HKCU\Control Panel\Desktop" /v FontSmoothing /t REG_SZ /d "2" /f >nul 2>&1
reg add "HKCU\Control Panel\Desktop" /v FontSmoothingType /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKCU\Control Panel\Desktop\WindowMetrics" /v MinAnimate /t REG_SZ /d "0" /f >nul 2>&1
echo       [成功] 视觉效果已调整为最佳性能（保留字体平滑）。
echo.

::==============================
:: 15. 启动速度、广告与休眠优化
::==============================
echo [15/18] 正在优化启动速度与系统广告...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize" /v StartupDelayInMSec /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v SystemPaneSuggestionsEnabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v SilentInstalledAppsEnabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v SubscribedContent-338388Enabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v SubscribedContent-310093Enabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v SubscribedContent-338390Enabled /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager" /v OemPreInstalledAppsEnabled /t REG_DWORD /d 0 /f >nul 2>&1
if "%HIBER%"=="Y" (
    powercfg /h off >nul 2>&1
    echo       [成功] 已关闭休眠，hiberfil.sys 空间已释放。
)
echo       [提示] 开机慢多半是启动项过多，请按 Ctrl+Shift+Esc 检查"启动"标签页。
echo.

::==============================
:: 16. 磁盘清理（C盘完整清理）
::==============================
echo [16/18] 正在执行磁盘清理（可能需要几分钟）...
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Temporary Files" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Recycle Bin" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Update Cleanup" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Setup Log Files" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Downloaded Program Files" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Thumbnail Cache" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Delivery Optimization Files" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Previous Installations" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches\Windows Error Reporting Files" /v StateFlags0001 /t REG_DWORD /d 2 /f >nul 2>&1
start /wait cleanmgr /sagerun:1 >nul 2>&1
echo       [成功] 磁盘清理已完成（含 Windows.old 残留与更新垃圾）。
echo.

::==============================
:: 17. SSD TRIM / 磁盘碎片整理
::==============================
echo [17/18] 正在执行 TRIM / 碎片整理...
defrag C: /O /U >nul 2>&1
echo       [成功] SSD 已执行 TRIM（HDD 已执行优化整理）。
echo.

::==============================
:: 18. 修复系统文件（先 DISM 后 SFC）
::==============================
echo [18/18] 正在检测并修复系统文件（可能需要 10~20 分钟，请耐心等待）...
DISM /Online /Cleanup-Image /RestoreHealth
sfc /scannow
echo       [完成] 系统文件扫描与修复结束（如有修复项，建议重启后复查一次）。
echo.

echo ============================================
echo           优化完成！
echo   注意 1：重启电脑以使网络重置等设置生效
echo   注意 2：Windows 更新已完全禁用，
echo           如需恢复见第 11 步的提示
echo ============================================
echo.
pause
exit /b 0
