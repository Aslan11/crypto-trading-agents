import os

import pytest
from temporalio.testing import docker_service
from temporalio.client import Client

from agents.feature_engineering_service import (
    _ensure_workflow as ensure_feature,
    FEATURE_WF_ID,
)
from agents.ensemble.ensemble_agent import (
    _ensure_workflow as ensure_ensemble,
    ENSEMBLE_WF_ID,
)
from agents.strategies.momentum_service import (
    _ensure_workflow as ensure_momentum,
    MOMENTUM_WF_ID,
)
from agents.execution.mock_exec_agent import (
    _ensure_workflow as ensure_ledger,
    LEDGER_WF_ID,
)


@pytest.mark.asyncio
async def test_agents_auto_start_workflows():
    async with docker_service() as svc:
        os.environ["TEMPORAL_ADDRESS"] = f"{svc.target_host}:{svc.grpc_port}"
        os.environ["TEMPORAL_NAMESPACE"] = svc.namespace
        client = await Client.connect(
            os.environ["TEMPORAL_ADDRESS"], namespace=os.environ["TEMPORAL_NAMESPACE"]
        )

        for ensure, wf_id in [
            (ensure_feature, FEATURE_WF_ID),
            (ensure_ensemble, ENSEMBLE_WF_ID),
            (ensure_momentum, MOMENTUM_WF_ID),
            (ensure_ledger, LEDGER_WF_ID),
        ]:
            await ensure(client)
            handle = client.get_workflow_handle(wf_id)
            desc = await handle.describe()
            assert desc.workflow_id == wf_id
