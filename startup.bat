@echo off
:: Tự động lấy đường dẫn tuyệt đối của thư mục hiện tại
set "CurrentDir=%~dp0"
set "TargetFile=%CurrentDir%Gemini-API.exe"
set "IconFile=%CurrentDir%logo.ico"

set "StartupFolder=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "vbs=%temp%\CreateShortcut.vbs"

:: Tạo script VBS tạm thời để dựng Shortcut (.lnk) có kèm Logo
echo Set oWS = CreateObject("WScript.Shell") > "%vbs%"
echo sLinkFile = "%StartupFolder%\Gemini-API.lnk" >> "%vbs%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%vbs%"
echo oLink.TargetPath = "%TargetFile%" >> "%vbs%"
echo oLink.WorkingDirectory = "%CurrentDir%" >> "%vbs%"

:: Kiểm tra nếu có file logo.ico trong thư mục thì mới gắn vào Shortcut
if exist "%IconFile%" (
    echo oLink.IconLocation = "%IconFile%" >> "%vbs%"
)

echo oLink.Save >> "%vbs%"

:: Thực thi file VBS và xóa nó sau khi hoàn tất
cscript //nologo "%vbs%"
del "%vbs%"

echo Da tu dong them run.exe vao Startup kem theo Logo thanh cong!
pause
