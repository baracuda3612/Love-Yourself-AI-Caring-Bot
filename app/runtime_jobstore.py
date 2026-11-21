"""Bounded reads of the persistent APScheduler store."""

from datetime import datetime, timedelta, timezone

from apscheduler.executors.base import BaseExecutor, MaxInstancesReachedError, run_job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.util import datetime_to_utc_timestamp
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.blocking_db import BlockingLane, DBBusyError


class RuntimeScheduler(BackgroundScheduler):
    def _process_jobs(self):
        try:
            return super()._process_jobs()
        except SQLAlchemyError:
            # APScheduler guards the due scan. Later job-store reads and
            # writes must also leave the polling thread alive on SQL failure.
            self._logger.exception("Scheduler job store operation failed")
            return self.jobstore_retry_interval


class RuntimeJobStore(SQLAlchemyJobStore):
    def __init__(self, *args, delivery_capacity=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._delivery_capacity = delivery_capacity

    def get_due_jobs(self, now):
        # APScheduler removes a once-only job after submission, even when an
        # executor rejects it. Leave unadmitted timer rows in this store.
        limit = min(64, self._delivery_capacity()) if self._delivery_capacity is not None else 64
        if not limit:
            return []
        columns = self.jobs_t.c
        due = (select(columns.id).where(columns.next_run_time <= datetime_to_utc_timestamp(now))
               .order_by(columns.next_run_time, columns.id).limit(limit))
        return self._get_jobs(columns.id.in_(due))

    def get_next_run_time(self):
        run_at = super().get_next_run_time()
        if run_at is None or self._delivery_capacity is None or self._delivery_capacity():
            return run_at
        # A worker also wakes the scheduler on completion. The timed backstop
        # keeps an already-due row from causing a polling spin during overload.
        return max(run_at, datetime.now(timezone.utc) + timedelta(seconds=1))

    def remove_legacy_completion_jobs(self):
        job_id = self.jobs_t.c.id
        with self.engine.begin() as connection:
            connection.execute(self.jobs_t.delete().where(or_(
                job_id.startswith("completion_", autoescape=True),
                job_id.startswith("completion_retry_", autoescape=True),
                job_id.startswith("completion_receipt_retry_", autoescape=True),
            )))

    def page(self, after, limit):
        columns = self.jobs_t.c
        query = select(columns.id, columns.job_state).order_by(columns.id).limit(limit)
        if after is not None:
            query = query.where(columns.id > after)
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        jobs = []
        invalid = []
        for row in rows:
            try:
                jobs.append(self._reconstitute_job(row.job_state))
            except Exception:
                self._logger.exception("Removing unreadable persisted job")
                invalid.append(row)
        if invalid:
            with self.engine.begin() as connection:
                for row in invalid:
                    connection.execute(self.jobs_t.delete().where(
                        columns.id == row.id, columns.job_state == row.job_state,
                    ))
        return jobs, rows[-1].id if len(rows) == limit else None


class DeliveryJobExecutor(BaseExecutor):
    """Admit only running timer jobs; rejected work stays pending in PostgreSQL."""

    def __init__(self, workers, overflow):
        super().__init__()
        self._lane = BlockingLane(workers, "delivery-timers")
        self._overflow = overflow

    def available(self):
        return self._lane.available()

    def _do_submit_job(self, job, run_times):
        try:
            future = self._lane.submit(run_job, job, job._jobstore_alias, run_times, self._logger.name)
        except DBBusyError:
            self._overflow.set()
            raise MaxInstancesReachedError(job) from None

        def finished(result):
            error = result.exception()
            if error is None:
                self._run_job_success(job.id, result.result())
            else:
                self._run_job_error(job.id, error, error.__traceback__)
            self._scheduler.wakeup()

        future.add_done_callback(finished)

    def shutdown(self, wait=True):
        self._lane.shutdown(wait=wait)
