from charge_agent.db import connect
from charge_agent.models import WorkflowInput
from charge_agent.storage import Store

with connect() as conn:
    print(Store(conn).create_run(WorkflowInput()))
