"""Admit polling updates by actor before they occupy a handler slot."""

import asyncio
from collections import Counter, deque

from aiogram import Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.methods import GetUpdates


class ActorFairDispatcher(Dispatcher):
    async def _polling(
        self, bot, polling_timeout=30, handle_as_tasks=True,
        backoff_config=DEFAULT_BACKOFF_CONFIG, allowed_updates=None,
        tasks_concurrency_limit=None, **kwargs,
    ):
        if not handle_as_tasks or tasks_concurrency_limit is None:
            await super()._polling(
                bot, polling_timeout=polling_timeout, handle_as_tasks=handle_as_tasks,
                backoff_config=backoff_config, allowed_updates=allowed_updates,
                tasks_concurrency_limit=tasks_concurrency_limit, **kwargs,
            )
            return

        await bot.me()
        pending = deque()
        running = set()
        active_by_actor = Counter()
        actor_slots = {}
        space = asyncio.Event()
        stopped = False
        last_seen = None

        async def before_next_batch(make_request, request_bot, method):
            if not isinstance(method, GetUpdates) or last_seen is None:
                return await make_request(request_bot, method)
            if pending:
                # An unconfirmed prefix makes getUpdates return immediately.
                # Pace repeated reads while allowing new actors into the batch.
                await asyncio.sleep(0.25)
            # Telegram acknowledges updates below the requested offset.
            # Never acknowledge an update that is still waiting for a slot.
            method.offset = pending[0][1].update_id if pending else last_seen + 1
            result = await make_request(request_bot, method)
            return [update for update in result if update.update_id > last_seen]

        def start_ready():
            for _ in range(len(pending)):
                actor_id, update = pending.popleft()
                # A Coach turn can release ordered ingress before its reply.
                # Allow the next action while bounding in-flight work per actor.
                if (len(running) >= tasks_concurrency_limit or actor_id in actor_slots
                        or active_by_actor[actor_id] >= 2):
                    pending.append((actor_id, update))
                    continue
                token = object()
                if actor_id is not None:
                    active_by_actor[actor_id] += 1
                    actor_slots[actor_id] = token
                task = asyncio.create_task(self._process_update(
                    bot=bot, update=update, **kwargs,
                    release_polling_actor=lambda actor=actor_id, slot=token: release_actor(actor, slot),
                ))
                running.add(task)
                self._handle_update_tasks.add(task)
                task.add_done_callback(lambda done, actor=actor_id, slot=token: finished(done, actor, slot))

        def release_actor(actor_id, token):
            if actor_slots.get(actor_id) is token:
                del actor_slots[actor_id]
            if not stopped:
                start_ready()
            space.set()

        def finished(task, actor_id, token):
            running.discard(task)
            self._handle_update_tasks.discard(task)
            if actor_id is not None:
                active_by_actor[actor_id] -= 1
                if not active_by_actor[actor_id]:
                    del active_by_actor[actor_id]
            release_actor(actor_id, token)

        bot.session.middleware.register(before_next_batch)
        try:
            async for update in self._listen_updates(
                bot, polling_timeout=polling_timeout, backoff_config=backoff_config,
                allowed_updates=allowed_updates,
            ):
                # Telegram returns at most 100 updates in a single response.
                while len(pending) >= 100:
                    space.clear()
                    await space.wait()
                event = update.callback_query or update.message
                actor_id = event.from_user.id if event and event.from_user else None
                pending.append((actor_id, update))
                last_seen = update.update_id
                start_ready()
        finally:
            stopped = True
            pending.clear()
            bot.session.middleware.unregister(before_next_batch)
