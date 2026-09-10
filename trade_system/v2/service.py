"""Bounded command actor: one live connection, no worker-owned connections."""
from concurrent.futures import Future
import itertools
from queue import PriorityQueue, Full
import threading
import time
import copy

from .accounts import import_snapshot, append_account_event, latest_account
from .decisions import DecisionService, RiskPolicy
from .domain import now_utc
from .storage import Store
from .strategies import record_signal, StrategyPolicy


class ServiceBusy(RuntimeError):
    pass


class Service:
    def __init__(self, path, *, capacity=64, critical_reserve=8, clock=now_utc):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('positive queue capacity required')
        if type(critical_reserve) is not int or critical_reserve < 1:
            raise ValueError('positive critical command reserve required')
        self.path, self.clock = path, clock
        self.capacity, self.critical_reserve = capacity, critical_reserve
        # One additional slot is exclusively for orderly stop/drain.
        self.queue = PriorityQueue(capacity + critical_reserve + 1)
        self.sequence = itertools.count()
        self.ready = Future()
        self.closed = False
        self.close_future = None
        self.submit_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.active_runtime = None
        self.last_runtime = None
        self.thread = threading.Thread(target=self._run, name='stock-data-v2-writer', daemon=True)
        self.thread.start()
        self.ready.result(timeout=30)

    def _run(self):
        active = None
        try:
            with Store(self.path, clock=self.clock) as store:
                self.ready.set_result(True)
                while True:
                    _, _, future, command, payload, deadline = self.queue.get()
                    active = future
                    if command == '__stop__':
                        future.set_result(True)
                        return
                    if not future.set_running_or_notify_cancel():
                        continue
                    if time.monotonic() >= deadline:
                        future.set_exception(TimeoutError('command expired before execution; not applied'))
                        continue
                    try:
                        started=time.monotonic()
                        with self.runtime_lock:
                            self.active_runtime={'command':command,'started':started,'deadline':deadline}
                        store.command_deadline=deadline
                        future.set_result(self._dispatch(store, command, payload))
                    except Exception as exc:
                        future.set_exception(exc)
                    finally:
                        store.command_deadline=None
                        with self.runtime_lock:
                            self.last_runtime={'command':command,'elapsed_seconds':time.monotonic()-started,
                                               'budget_exceeded':time.monotonic()>=deadline}
                            self.active_runtime=None
        except BaseException as exc:
            # Exclude submit while closing and draining. Otherwise a producer
            # can enqueue after the drain, leaving its Future unresolved.
            with self.submit_lock:
                self.closed = True
                pending = []
                while not self.queue.empty():
                    pending.append(self.queue.get_nowait()[2])
            if not self.ready.done():
                self.ready.set_exception(exc)
            if active is not None:
                pending.append(active)
            for future in pending:
                if not future.done():
                    future.set_exception(RuntimeError('writer stopped; inspect durable state before retry'))

    @staticmethod
    def _dispatch(store, command, data):
        store.check_owner()
        if command == 'product':
            return store.register_product(**data)
        if command == 'ingest':
            return store.ingest(**data)
        if command == 'freeze':
            return store.freeze(**data)
        if command == 'account_import':
            return import_snapshot(store, **data)
        if command == 'account_event':
            return append_account_event(store, **data)
        if command == 'account':
            return latest_account(store, **data)
        if command == 'signal':
            args = dict(data)
            args['policy'] = StrategyPolicy(**args['policy'])
            return record_signal(store, **args)
        if command == 'context_import':
            from .context_storage import import_context
            return import_context(store, **data)
        if command == 'market_event':
            from .event_bridge import ingest_event
            return ingest_event(store, **data)
        if command == 'event_signal':
            from .event_bridge import event_signal
            return event_signal(store, **data)
        if command == 'daily_case':
            from .daily_session import link_case
            return link_case(store, **data)
        if command == 'paper_open':
            from .paper_storage import open_paper
            return open_paper(store, **data)
        if command == 'paper_event':
            from .paper_storage import apply_paper_event
            return apply_paper_event(store, **data)
        if command == 'paper_buy':
            from .paper_storage import submit_confirmed_buy
            return submit_confirmed_buy(store, **data)
        if command == 'paper_sell':
            from .exit_policy import submit_confirmed_sell
            return submit_confirmed_sell(store, **data)
        if command == 'attribution':
            from .attribution import project_attribution
            return project_attribution(store, **data)
        if command == 'paper_status':
            from .paper_storage import load_paper
            return load_paper(store, **data).summary()
        if command == 'paper_verify':
            from .paper_storage import load_paper
            return load_paper(store, data['account_id'], full_replay=True).summary()
        if command == 'context':
            from .context_storage import get_context
            return get_context(store, **data)
        if command in ('close_unsent','reconcile_paper_unsent'):
            import json
            row = store.con.execute('SELECT payload FROM operator_action WHERE action_id=?',
                                    [data['confirmation_request_id']]).fetchone()
            if not row:
                raise ValueError('unknown confirmation')
            policy = json.loads(row[0]).get('result',{}).get('policy')
            if not policy:
                raise ValueError('original paper confirmation policy required')
            return getattr(DecisionService(store,RiskPolicy(**policy)),command)(**data)
        if command in ('propose', 'confirm', 'propose_exit', 'mark_exit_unknown'):
            args = dict(data)
            decisions = DecisionService(store, RiskPolicy(**args.pop('risk_policy')))
            return getattr(decisions, command)(**args)
        if command == 'status':
            return {'mode': 'paper_only', 'execution_ready': False, 'broker_routing': 'disabled',
                    'fact_count': store.con.execute('SELECT count(*) FROM fact').fetchone()[0]}
        if command == 'action':
            import json
            row = store.con.execute('SELECT payload FROM operator_action WHERE action_id=?', [data['request_id']]).fetchone()
            return json.loads(row[0]) if row else None
        if command == 'review':
            from .reporting import project_review
            return project_review(store, **data)
        if command in ('paper_plan_export','paper_plan_confirm','paper_desk_review'):
            from .operator_workflow import export_plan, confirm_plan, review_desk
            return {'paper_plan_export':export_plan,'paper_plan_confirm':confirm_plan,
                    'paper_desk_review':review_desk}[command](store,**data)
        raise ValueError('unsupported service command')

    def health(self):
        """Non-queued telemetry remains available during a slow owning command.

        Deadlines are cooperative safe-boundary checks, not hard realtime.
        A slow native call is never killed or treated as not applied.
        """
        with self.runtime_lock:
            active=dict(self.active_runtime) if self.active_runtime else None
            last=dict(self.last_runtime) if self.last_runtime else None
        now=time.monotonic()
        if active:
            active={'command':active['command'],'elapsed_seconds':now-active['started'],
                    'deadline_exceeded':now>=active['deadline']}
        return {'writer_alive':self.thread.is_alive(),'admission_closed':self.closed,
                'queued':self.queue.qsize(),'active':active,'last':last,
                'deadline_mode':'cooperative_safe_boundaries_not_hard_realtime','execution_ready':False}

    def submit(self, command, *, budget_seconds=30, **payload):
        if command == '__stop__' or not 0 < budget_seconds <= 300:
            raise ValueError('invalid command/budget')
        # Account and confirmation jobs precede new discoveries; cold model
        # training is not an actor command and cannot block this queue.
        priority = 0 if command in ('account_import', 'account_event', 'account', 'confirm','paper_event','paper_buy',
                                    'paper_sell','propose_exit','mark_exit_unknown','paper_plan_confirm','close_unsent','reconcile_paper_unsent') else 1
        future = Future()
        with self.submit_lock:
            if self.closed or not self.thread.is_alive():
                raise RuntimeError('service closed')
            limit = self.capacity + (self.critical_reserve if priority == 0 else 0)
            if self.queue.qsize() >= limit:
                raise ServiceBusy('admission capacity reached; critical and shutdown capacity remain reserved')
            try:
                self.queue.put_nowait((priority, next(self.sequence), future, command, copy.deepcopy(payload),
                                       time.monotonic() + budget_seconds))
            except Full as exc:
                raise ServiceBusy('queue full; caller must retain input and retry with same event id') from exc
        return future

    def close(self):
        with self.submit_lock:
            if not self.closed:
                self.closed = True
                self.close_future = Future()
                self.queue.put_nowait((2, next(self.sequence), self.close_future, '__stop__', {}, float('inf')))
        # A timed-out close is retryable: never return merely because closed
        # means no new admissions. The owning thread may still be draining.
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise TimeoutError('writer still draining; do not launch another writer')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
