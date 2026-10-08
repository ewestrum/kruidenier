import asyncio

from app.ah.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_spaces_consecutive_requests() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    async with limiter:
        clock.now += 0.2  # request takes 0.2s
    async with limiter:
        pass
    assert clock.sleeps == [1.0]


async def test_no_wait_when_interval_already_passed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    async with limiter:
        pass
    clock.now += 5
    async with limiter:
        pass
    assert clock.sleeps == []


async def test_never_two_requests_in_flight() -> None:
    limiter = RateLimiter(0.0)
    in_flight = 0
    peak = 0

    async def request() -> None:
        nonlocal in_flight, peak
        async with limiter:
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1

    await asyncio.gather(*(request() for _ in range(10)))
    assert peak == 1
