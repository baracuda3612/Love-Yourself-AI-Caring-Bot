# Новий файл app/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
import os
from app.config import DB_URL

# Використаємо окремий файл для jobstore поруч із основною БД (jobs.sqlite)
# Якщо DB_URL = sqlite:///./ly_bot.db -> jobs at ./jobs.sqlite
if DB_URL.startswith("sqlite:///"):
    base_path = DB_URL.replace("sqlite:///", "")
    base_dir = os.path.dirname(os.path.abspath(base_path)) or "."
    jobs_db_path = os.path.join(base_dir, "jobs.sqlite")
    jobstore_url = f"sqlite:///{jobs_db_path}"
else:
    # Якщо Postgres/інші, можна використовувати той же DB_URL
    jobstore_url = DB_URL

jobstores = {
    'default': SQLAlchemyJobStore(url=jobstore_url)
}

scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

def init_scheduler():
    # Запускати при старті програми
    scheduler.start()

def shutdown_scheduler():
    scheduler.shutdown(wait=True)

# Утиліти для додавання/видалення job-ів
def add_job(func, trigger, id=None, **kwargs):
    return scheduler.add_job(func, trigger, id=id, **kwargs)

def remove_job(job_id):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

def reschedule_job(job_id, **trigger_args):
    try:
        scheduler.reschedule_job(job_id, **trigger_args)
    except Exception:
        raise
