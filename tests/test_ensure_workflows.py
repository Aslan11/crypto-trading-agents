import os

import pytest
from temporalio.testing import WorkflowEnvironment

from agents.feature_engineering_service import (
    _ensure_workflow as ensure_feature,
    FEATURE_WF_ID,
)


@pytest.mark.asyncio
async def test_agents_auto_start_workflows():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        os.environ["TEMPORAL_ADDRESS"] = env.client.config()["target"]
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        client = env.client

        for ensure, wf_id in [
            (ensure_feature, FEATURE_WF_ID),
        ]:
            await ensure(client)
            handle = client.get_workflow_handle(wf_id)
            desc = await handle.describe()
            assert desc.workflow_id == wf_id
