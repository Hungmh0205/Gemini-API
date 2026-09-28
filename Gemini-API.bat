::[Bat To Exe Converter]
::
::YAwzoRdxOk+EWAjk
::fBw5plQjdCuDJHeB4wI2JxdQSRa+G263D7sVpcnLxtaLrUoSGus8d+8=
::YAwzuBVtJxjWCl3EqQJgSA==
::ZR4luwNxJguZRRnk
::Yhs/ulQjdF+5
::cxAkpRVqdFKZSTk=
::cBs/ulQjdF+5
::ZR41oxFsdFKZSDk=
::eBoioBt6dFKZSTk=
::cRo6pxp7LAbNWATEpCI=
::egkzugNsPRvcWATEpCI=
::dAsiuh18IRvcCxnZtBJQ
::cRYluBh/LU+EWAnk
::YxY4rhs+aU+JeA==
::cxY6rQJ7JhzQF1fEqQJQ
::ZQ05rAF9IBncCkqN+0xwdVs0
::ZQ05rAF9IAHYFVzEqQJQ
::eg0/rx1wNQPfEVWB+kM9LVsJDGQ=
::fBEirQZwNQPfEVWB+kM9LVsJDGQ=
::cRolqwZ3JBvQF1fEqQJQ
::dhA7uBVwLU+EWDk=
::YQ03rBFzNR3SWATElA==
::dhAmsQZ3MwfNWATElA==
::ZQ0/vhVqMQ3MEVWAtB9wSA==
::Zg8zqx1/OA3MEVWAtB9wSA==
::dhA7pRFwIByZRRnk
::Zh4grVQjdCuDJHeB4wI2JxdQSRa+G263D7sVpcnLxtagp0AUWutyWb/ylLGWJYA=
::YB416Ek+ZG8=
::
::
::978f952a14a936cc963da21a135fa983
@echo off
setlocal
cd /d "%~dp0"

rem Chay server bang Python portable trong thu muc .\python_portable
rem (ban embeddable cua Python, khong can cai Python len may).
rem Copy ca thu muc nay sang may khuc, chay file nay la chay.

if not exist "python_portable\python.exe" (
    echo [ERROR] Khong tim thay python_portable\python.exe
    echo Ban can tai ban embeddable Python 3.13 vao thu muc python_portable\
    echo va giai nen: python_portable\python.exe -m pip install -r requirements.txt
    exit /b 1
)

rem QUAN TRONG: bo qua site-packages cua user Windows.
rem File python313._pth khoa sys.path, nhung `import site` van tu them
rem C:\Users\<ten>\AppData\Roaming\Python\... vao. Doi voi ban "portable" thi
rem do la tua duong: no se lam folder nay phu thuoc may da tao ra no, va no
rem "mua" pywin32 cua user dang chay, gay loi khi chuyen may.
set PYTHONNOUSERSITE=1

rem -X utf8: log tieng Viet / loi khong loi dau ? khi in ra console Windows.
"python_portable\python.exe" -X utf8 tray.py %*
