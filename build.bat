@echo off
echo Building Kotak Dashboard EXE...

:: Clean previous build
rd /s /q build
rd /s /q dist
del /q *.spec

:: Build with PyInstaller
:: --noconfirm: overwrite output
:: --onefile: single exe (optional, but requested "single file" effectively)
:: --windowed: no console (optional, but user likes logs, maybe keep console? User said "like software installation", usually no console. I'll use --console for now as per plan, but let's stick to windowed if it's "software". 
:: Wait, implementation plan said "Decision: Keep console for now". Sticking to that. 
:: --add-data: resources;resources (separator is ; on windows)
:: --hidden-import: neo_api_client

pyinstaller --noconfirm --onefile --console ^
    --name "KotakDashboard" ^
    --add-data "resources;resources" ^
    --hidden-import "neo_api_client" ^
    kotak_dahboard.py

echo Build complete. Check dist folder.
pause
