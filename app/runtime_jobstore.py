"""Bounded reads of the persistent APScheduler store."""

from apscheduler.executors.base import BaseExecutor, MaxInstancesReachedError, run_job
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.util import datetime_to_utc_timestamp
from sqlalchemy import or_, select

from app.blocking_db import BlockingLane, DBBusyError


class RuntimeJobStore(SQLAlchemyJobStore):
    def get_due_jobs(self, now):
        columns = self.jobs_t.c
        due = (select(columns.id).where(columns.next_run_time <= datetime_to_utc_timestamp(now))
               .order_by(columns.next_run_time, columns.id).limit(64))
        return self._get_jobs(columns.id.in_(due))

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
        return [self._reconstitute_job(row.job_state) for row in rows]


class DeliveryJobExecutor(BaseExecutor):
    """Admit only running timer jobs; rejected work stays pending in PostgreSQL."""

    def __init__(self, workers, overflow):
        super().__init__()
        self._lane = BlockingLane(workers, "delivery-timers")
        self._overflow = overflow

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

        future.add_done_callback(finished)

    def shutdown(self, wait=True):
        self._lane.shutdown(wait=wait)
