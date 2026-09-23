@echo off
rem This file is saved as CP949 (Korean Windows' default codepage), not
rem UTF-8 -- chcp 65001 was unreliable here. It also avoids
rem "setlocal enabledelayedexpansion" (no "!var!" anywhere) and multi-line
rem if/else ( ... ) blocks: both interact badly with CP949 in this
rem environment, since a Hangul syllable's trailing byte can coincide with
rem a batch special character (!, ^, %) and corrupt the parse from that
rem point in the line onward. Everything below uses single-line
rem "if errorlevel N goto label" instead, which cmd reads and runs one
rem line at a time with no block-level pre-parsing to get confused by.

set "MODEL_NAME=hf.co/hell0ks/ja-ko-vn-12b-v2-gguf:Q5_K_M"

echo ================================================
echo  JP-KO Trans (일본어 게임 한국어화 도구) - 처음 설치
echo ================================================
echo.
echo 이 스크립트는 다음을 자동으로 확인/설치합니다:
echo   1. Ollama (로컬 LLM 실행 프로그램)
echo   2. 번역용 AI 모델 (약 8.4GB, 인터넷 필요 - 이미 있으면 건너뜀)
echo.
pause

where ollama >nul 2>nul
if errorlevel 1 goto need_ollama
echo [확인] Ollama가 이미 설치되어 있습니다.
goto ollama_installed

:need_ollama
where winget >nul 2>nul
if errorlevel 1 goto no_winget

echo [설치] Ollama를 설치합니다...
winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto ollama_install_failed

rem winget 설치 직후에는 이 창의 PATH가 아직 안 바뀐 상태라 "ollama"
rem 명령을 못 찾을 수 있음 -- 창을 새로 열지 않고 바로 이어서 쓸 수
rem 있게 설치 위치를 이 창의 PATH에 바로 추가해줌
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"

where ollama >nul 2>nul
if errorlevel 1 goto ollama_not_recognized

echo [완료] Ollama 설치됨. 서버가 뜰 때까지 잠시 대기합니다...
timeout /t 5 >nul
goto ollama_installed

:no_winget
echo.
echo [오류] winget을 찾을 수 없어 자동 설치를 진행할 수 없습니다.
echo Microsoft Store에서 "앱 설치 관리자"를 업데이트하거나
echo https://ollama.com 에서 직접 다운로드해서 설치해주세요.
pause
exit /b 1

:ollama_install_failed
echo.
echo [오류] Ollama 자동 설치에 실패했습니다.
echo https://ollama.com 에서 직접 다운로드해서 설치해주세요.
pause
exit /b 1

:ollama_not_recognized
echo.
echo [오류] Ollama는 설치됐지만 이 창에서 인식하지 못했습니다.
echo 이 창을 닫고 새로 열어서 스크립트를 다시 실행해주세요.
pause
exit /b 1

:ollama_installed
echo.
echo [확인] Ollama 서버 응답을 확인합니다...
set "OLLAMA_TRIES=0"

:wait_ollama_loop
ollama list >nul 2>nul
if not errorlevel 1 goto ollama_ready
set /a OLLAMA_TRIES=OLLAMA_TRIES+1
if %OLLAMA_TRIES% GEQ 10 goto ollama_not_ready
timeout /t 2 >nul
goto wait_ollama_loop

:ollama_not_ready
echo.
echo [오류] Ollama 서버가 응답하지 않습니다. Ollama를 한 번 실행해서 켠 뒤 다시 시도해주세요.
pause
exit /b 1

:ollama_ready
echo [확인] Ollama 서버 정상 동작 중.

echo.
echo [확인] 번역 모델이 이미 있는지 확인합니다...
ollama list | findstr /c:"%MODEL_NAME%" >nul 2>nul
if errorlevel 1 goto need_model
echo [확인] 번역 모델이 이미 설치되어 있습니다. 다운로드를 건너뜁니다.
goto model_ready

:need_model
echo [설치] 번역 모델을 받습니다 (용량이 커서 시간이 걸릴 수 있습니다)...
ollama pull %MODEL_NAME%
if errorlevel 1 goto model_download_failed
goto model_ready

:model_download_failed
echo.
echo [오류] 모델 다운로드에 실패했습니다. 인터넷 연결을 확인 후 다시 시도해주세요.
pause
exit /b 1

:model_ready
echo.
echo [설정] 동시 번역 처리량 설정을 적용합니다...
setx OLLAMA_NUM_PARALLEL "4" >nul
setx OLLAMA_CONTEXT_LENGTH "4096" >nul

if exist "JP-KO_Trans.exe" goto exe_ready

echo.
echo [빌드] 실행 파일이 없어 소스에서 새로 빌드합니다...
where python >nul 2>nul
if errorlevel 1 goto need_python

echo [빌드] 필요한 패키지를 설치합니다 (처음 한 번만, 몇 분 걸릴 수 있습니다)...
python -m pip install --user --quiet pyinstaller customtkinter fonttools brotli requests anthropic
if errorlevel 1 goto pip_failed

pushd src
pyinstaller --noconfirm --onefile --windowed --name "JP-KO_Trans" --collect-all customtkinter --add-data "plugins;plugins" --distpath .. gui.py
if errorlevel 1 goto build_failed
popd
echo [빌드] 완료.
goto exe_ready

:need_python
echo.
echo [오류] Python이 설치되어 있지 않아 실행 파일을 빌드할 수 없습니다.
echo https://python.org 에서 Python 3.10 이상을 설치할 때 "Add python.exe to PATH"를
echo 꼭 체크한 뒤, 이 스크립트를 다시 실행해주세요.
pause
exit /b 1

:pip_failed
echo.
echo [오류] 필요한 패키지 설치에 실패했습니다. 인터넷 연결을 확인 후 다시 시도해주세요.
pause
exit /b 1

:build_failed
popd
echo.
echo [오류] 빌드에 실패했습니다. 위 오류 메시지를 확인해주세요.
pause
exit /b 1

:exe_ready
echo.
echo ================================================
echo  설치 완료!
echo  JP-KO_Trans.exe 를 실행해서 사용하세요.
echo  (방금 설정한 환경변수가 확실히 반영되려면
echo   컴퓨터를 한 번 재시작하는 것을 권장합니다)
echo ================================================
pause
