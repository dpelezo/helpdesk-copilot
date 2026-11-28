"""The Temporal worker for Workflow A (ADR 0004): today, the knowledge review (#28)."""

import asyncio

from dotenv import load_dotenv
from temporalio.worker import Worker

from app.workflows import review

load_dotenv()  # loads .env before anything else


async def main() -> None:
    worker = Worker(
        await review.connect(),
        task_queue=review.TASK_QUEUE,
        workflows=[review.ReviewWorkflow],
        activities=review.ACTIVITIES,
    )
    print(f"Worker started on task queue {review.TASK_QUEUE}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
