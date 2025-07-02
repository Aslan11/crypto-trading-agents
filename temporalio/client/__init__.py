import asyncio
from types import SimpleNamespace

class RPCStatusCode:
    NOT_FOUND = 5

class RPCError(Exception):
    def __init__(self, message, status=RPCStatusCode.NOT_FOUND, details=None):
        super().__init__(message)
        self.status = status
        self.details = details

class WorkflowExecutionStatus:
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'

class _Handle:
    def __init__(self, workflow_id):
        self.workflow_id = workflow_id
        self.run_id = 'run-1'
        self._status = WorkflowExecutionStatus.COMPLETED
    async def describe(self):
        return SimpleNamespace(status=self._status)
    async def result(self):
        return None
    async def query(self, name):
        return None

class Client:
    @classmethod
    async def connect(cls, address, namespace='default'):
        return cls()
    def __init__(self):
        self.workflows = {}
    async def start_workflow(self, fn, *args, id=None, task_queue=None, **kw):
        handle = _Handle(id or 'wf')
        self.workflows[handle.workflow_id] = handle
        return handle
    def get_workflow_handle(self, workflow_id, run_id=None):
        return self.workflows.get(workflow_id, _Handle(workflow_id))

class Schedule: ...
class ScheduleActionStartWorkflow:
    def __init__(self, *args, **kwargs):
        pass
class ScheduleIntervalSpec:
    def __init__(self, every):
        self.every = every
class ScheduleSpec:
    def __init__(self, intervals):
        self.intervals = intervals
