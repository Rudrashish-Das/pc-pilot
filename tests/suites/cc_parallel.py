"""Claude Code job scheduling: Anthropic jobs in different chats run in parallel, local-model jobs (ollama/custom)
run one at a time, and jobs in the same chat always wait for each other. Claude Code itself is faked."""
import asyncio

from _setup import B  # noqa  (first: isolates the bot)

fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what)
    fails += not ok


async def fake_execute(job):
    job.status = "running"
    job.peak = len(B._cc_running)
    await asyncio.sleep(0.2)


async def fake_finalize(job):
    pass


B._execute, B._finalize = fake_execute, fake_finalize


class Ch:
    def __init__(self, i):
        self.id = i


async def run(*jobs):
    made = [B.CCJob(id=f"j{n}", channel=Ch(ch), task="t", snap=B.CCSnap(backend, "m", "read", "default"), user_id=1)
            for n, (ch, backend) in enumerate(jobs)]
    await asyncio.gather(*(B._run_job_inner(j) for j in made))
    return max(j.peak for j in made)


async def main():
    check(await run((1, "anthropic"), (2, "anthropic"), (3, "anthropic")) == 3, "Anthropic jobs in different chats run together")
    check(await run((1, "ollama"), (2, "ollama")) == 1, "local-model jobs run one at a time")
    check(await run((1, "custom"), (2, "anthropic")) == 2, "an Anthropic job doesn't wait for a local one")
    check(await run((1, "anthropic"), (1, "anthropic")) == 1, "jobs in the same chat wait for each other")
    check(not B._cc_running and B._cc_waiting == 0, "nothing left running or queued")


asyncio.run(main())
raise SystemExit(1 if fails else 0)
