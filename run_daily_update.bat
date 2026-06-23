@echo off
REM Daily NCAAF play-by-play updater (for Windows Task Scheduler).
REM Auto-detects the current season and updates the merged season CSV.
REM All activity is appended to data\update_log.txt.

cd /d "C:\Users\colte\Claude Workspace\Sportsbetting\NCAAF"
"C:\Users\colte\anaconda3\python.exe" update_season.py >> "data\update_stdout.txt" 2>&1
