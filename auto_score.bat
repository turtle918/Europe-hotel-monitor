@echo off
cd /d D:\europe-hotel-monitor
git pull
python ai_evaluator.py
git add booking_data.db
git commit -m "自动更新本地 AI 评分"
git push