"""
Gunicorn configuration file
Usage: gunicorn -c gunicorn_config.py server:app
"""
bind = "0.0.0.0:5000"
workers = 2
threads = 4
timeout = 120          # 預設 30 秒會讓 /api/recommend 被砍，改 120 秒
graceful_timeout = 30
keepalive = 5
worker_class = "gthread"
loglevel = "info"
accesslog = "-"
errorlog = "-"
