@echo off
REM Soqchi — osilib qolgan ishlarni kuzatadi va navbatni ochib yuboradi.
REM Bot bilan BIRGA ishga tushirilishi kerak. Har 10 daqiqada tekshiradi,
REM 30 daqiqadan ortiq qimirlamagan ishni failed qiladi (qayta navbatga qo'ymaydi).
cd /d "%~dp0"
.venv\Scripts\python.exe soqchi.py --kutish 600 --chegara 30
pause
