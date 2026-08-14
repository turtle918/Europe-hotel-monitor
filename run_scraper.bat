cd /d D:\hotel-monitor
rem API Key 从环境变量 DEEPSEEK_API_KEY 读取（请勿在此硬编码密钥）
python booking_scraper.py
python ai_evaluator.py