# Установщик af-bot (запускается ОДИН раз). Ставит Chrome + Python + пакеты + агент + автозапуск.
# sa.json должен быть уже положен в %USERPROFILE%\asfarm (это делает строка-запускатор до iex).
$ErrorActionPreference="Continue"
$d="$env:USERPROFILE\asfarm"; New-Item -ItemType Directory -Force -Path $d | Out-Null
$B="https://raw.githubusercontent.com/nikooolechka/wb-oz-monitor/main/afbot"

Write-Host "[1/6] Google Chrome..."
$chrome="C:\Program Files\Google\Chrome\Application\chrome.exe"
if(-not (Test-Path $chrome)){
  try{
    $ci="$env:TEMP\chr.msi"
    Invoke-WebRequest "https://dl.google.com/tag/s/dl/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile $ci -UseBasicParsing
    Start-Process msiexec -ArgumentList "/i `"$ci`" /qn /norestart" -Wait
  }catch{ Write-Host "Chrome install err: $_" }
}
if(Test-Path $chrome){ Write-Host "   Chrome OK" } else { Write-Host "   Chrome НЕ встал — проверь вручную" }

Write-Host "[2/6] Python 3.12..."
$py="$d\py\python.exe"
if(-not (Test-Path $py)){
  try{
    $pi="$env:TEMP\py.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $pi -UseBasicParsing
    Start-Process $pi -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_pip=1 Include_launcher=0 TargetDir=`"$d\py`"" -Wait
  }catch{ Write-Host "Python install err: $_" }
}
if(Test-Path $py){ Write-Host "   Python OK" } else { Write-Host "   Python НЕ встал"; return }

Write-Host "[3/6] Библиотеки (undetected-chromedriver, selenium, google-auth, requests)..."
& $py -m pip install --upgrade pip 2>&1 | Out-Null
& $py -m pip install --upgrade undetected-chromedriver selenium google-auth requests urllib3 2>&1 | Out-Null
Write-Host "   пакеты поставлены"

Write-Host "[4/6] Агент + сборщик карточек из бакета..."
Invoke-WebRequest "$B/afbot_agent.py" -OutFile "$d\afbot_agent.py" -UseBasicParsing
Invoke-WebRequest "$B/oz_card_pc.py" -OutFile "$d\oz_card_pc.py" -UseBasicParsing
if(Test-Path "$d\sa.json"){ Write-Host "   sa.json на месте" } else { Write-Host "   ВНИМАНИЕ: sa.json НЕТ — агент не сможет писать в таблицу" }

Write-Host "[5/6] Автозапуск при входе..."
$st=[Environment]::GetFolderPath("Startup")
"@echo off`r`nstart `"`" /min `"$py`" `"$d\afbot_agent.py`"" | Out-File -FilePath "$st\afbot_agent.cmd" -Encoding ascii

Write-Host "[6/6] Запуск агента сейчас..."
try{ Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -like "*afbot_agent*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }catch{}
Start-Process -WindowStyle Hidden $py -ArgumentList "`"$d\afbot_agent.py`""
Write-Host ""
Write-Host "================ ГОТОВО ================"
Write-Host "Агент af-bot запущен. Дальше всё делаю я из облака (headless), мак не нужен."
Write-Host "Можешь закрыть это окно."
