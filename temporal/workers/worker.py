from dotenv import load_dotenv
import asyncio, os
from temporalio.client import Client
from temporalio.worker import Worker
from langsmith import traceable
from app.workflows.kb_workflows import (
    KBCreationWorkflow,
    fetch_existing_kb,
    run_agent_activity,
    save_draft_to_db,
    notify_human_agent,
    publish_kb_article,
    update_draft_with_feedback,
    reject_kb_article,
)

load_dotenv()  # loads .env before anything else


@traceable
async def main():
    host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY", "")

    if api_key:
        client = await Client.connect(
            host,
            namespace=namespace,
            api_key=api_key,
            tls=True,
        )
    else:
        client = await Client.connect(host, namespace=namespace)

    worker = Worker(
        client,
        task_queue="kb-task-queue",
        workflows=[KBCreationWorkflow],
        activities=[
            fetch_existing_kb,
            run_agent_activity,
            save_draft_to_db,
            notify_human_agent,
            publish_kb_article,
            update_draft_with_feedback,
            reject_kb_article,
        ],
    )

    print(f"🚀 Worker started — host: {host} namespace: {namespace}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
