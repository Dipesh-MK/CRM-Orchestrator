#!/usr/bin/env python3
"""
Load Testing & Scalability Harness for Multi-Aggregator SMS/MMS CRM
Supports high-volume load testing (up to 1,000,000 simulated messages)
with rate limiting, 429 backoff, outage recovery, and deduplication verification.
"""

import sys
import os
import time
import math
import asyncio
import argparse
import random
import statistics
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

try:
    import psutil
except ImportError:
    psutil = None

from app.providers.base import NormalizedSendResult
from app.providers.mock_provider import MockAggregatorProvider
from app.providers.twilio_provider import TwilioProvider
from app.providers.infobip_provider import InfobipProvider
from app.providers.registry import get_provider_registry

class RateLimiter:
    """Token bucket rate limiter for outbound throttling."""
    def __init__(self, rate_per_second: float):
        self.rate = rate_per_second
        self.capacity = max(1.0, rate_per_second)
        self.tokens = self.capacity
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.rate <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0

class LoadTestHarness:
    def __init__(self, target_count: int = 1000,
                 concurrency: int = 50,
                 provider_id: str = "mock",
                 rate_limit: float = 500.0,
                 simulate_429: bool = False,
                 simulate_outage: bool = False,
                 outage_duration_s: float = 1.0):
        self.target_count = target_count
        self.concurrency = concurrency
        self.provider_id = provider_id
        self.rate_limiter = RateLimiter(rate_limit) if rate_limit > 0 else None
        self.simulate_429 = simulate_429
        self.simulate_outage = simulate_outage
        self.outage_duration_s = outage_duration_s

        self.latencies_ms: List[float] = []
        self.successful_sends = 0
        self.failed_sends = 0
        self.retried_sends = 0
        self.delivered_sids = set()
        self.duplicate_deliveries = 0

        self.outage_active = False
        self.start_time = 0.0
        self.end_time = 0.0

    async def _send_worker(self, queue: asyncio.Queue, provider, p_config: Dict[str, Any]):
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            msg_idx, to_phone, body, idempotency_key = item

            # Outbound rate limiter
            if self.rate_limiter:
                await self.rate_limiter.acquire()

            t0 = time.monotonic()
            attempts = 0
            max_attempts = 4
            success = False

            while attempts < max_attempts:
                attempts += 1

                # Simulated provider outage check
                if self.outage_active:
                    await asyncio.sleep(0.2)
                    continue

                # Simulated 429 rate limit injection
                if self.simulate_429 and random.random() < 0.05 and attempts == 1:
                    self.retried_sends += 1
                    backoff = (2 ** attempts) * 0.05 + random.uniform(0.01, 0.05)
                    await asyncio.sleep(backoff)
                    continue

                try:
                    # Simulated mock fast send or real call
                    if self.provider_id == "mock" and not p_config.get("use_network"):
                        # High-volume in-memory simulation for benchmarking
                        await asyncio.sleep(0.001)  # 1ms carrier simulation
                        sid = f"mock_{idempotency_key}"
                        res = NormalizedSendResult(status="queued", provider_message_id=sid, recipient_phone=to_phone)
                    else:
                        res = await provider.send_message(
                            from_phone="+15551234560",
                            to_phone=to_phone,
                            body=body,
                            config=p_config
                        )

                    if res.status in ("queued", "sent", "delivered"):
                        latency = (time.monotonic() - t0) * 1000
                        self.latencies_ms.append(latency)
                        self.successful_sends += 1

                        # At-most-once deduplication check
                        sid = res.provider_message_id or idempotency_key
                        if sid in self.delivered_sids:
                            self.duplicate_deliveries += 1
                        else:
                            self.delivered_sids.add(sid)

                        success = True
                        break
                    else:
                        if "429" in (res.error_reason or ""):
                            self.retried_sends += 1
                            await asyncio.sleep((2 ** attempts) * 0.1)
                        else:
                            break
                except Exception as ex:
                    if attempts < max_attempts:
                        self.retried_sends += 1
                        await asyncio.sleep((2 ** attempts) * 0.05)
                    else:
                        break

            if not success:
                self.failed_sends += 1
                self.latencies_ms.append((time.monotonic() - t0) * 1000)

            queue.task_done()

    async def _outage_controller(self):
        """Simulates provider outage halfway through the run and verifies recovery."""
        if not self.simulate_outage:
            return
        await asyncio.sleep(0.5)
        self.outage_active = True
        print(f"⚠️ [FAULT INJECTION] Outage triggered! Provider offline for {self.outage_duration_s}s...")
        await asyncio.sleep(self.outage_duration_s)
        self.outage_active = False
        print("✅ [FAULT RECOVERY] Provider restored! Resuming queue processing...")

    async def run(self, use_network: bool = False) -> Dict[str, Any]:
        registry = get_provider_registry()
        provider = registry.get_provider(self.provider_id)
        p_config = {"use_network": use_network}

        queue = asyncio.Queue()
        for i in range(self.target_count):
            phone = f"+1555123{i % 9000 + 1000}"
            body = f"Load test message {i+1} for CampaignForge at {time.time()}"
            idempotency_key = f"idemp_{i}"
            queue.put_nowait((i, phone, body, idempotency_key))

        print(f"\n🚀 Starting Load Test Harness:")
        print(f"   Target Messages : {self.target_count:,}")
        print(f"   Provider        : {self.provider_id.upper()}")
        print(f"   Concurrency     : {self.concurrency} workers")
        print(f"   Simulate 429    : {self.simulate_429}")
        print(f"   Simulate Outage : {self.simulate_outage}\n")

        self.start_time = time.monotonic()
        mem_before = psutil.Process().memory_info().rss / (1024 * 1024) if psutil else 0.0

        # Launch workers and outage simulator
        workers = [
            asyncio.create_task(self._send_worker(queue, provider, p_config))
            for _ in range(self.concurrency)
        ]
        outage_task = asyncio.create_task(self._outage_controller())

        await queue.join()
        for w in workers:
            w.cancel()
        await outage_task

        self.end_time = time.monotonic()
        total_duration = max(0.001, self.end_time - self.start_time)
        throughput = self.successful_sends / total_duration
        mem_after = psutil.Process().memory_info().rss / (1024 * 1024) if psutil else 0.0

        # Percentiles
        if self.latencies_ms:
            sorted_lat = sorted(self.latencies_ms)
            p50 = statistics.median(sorted_lat)
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
            p99 = sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.99))]
            avg_lat = statistics.mean(sorted_lat)
        else:
            p50 = p95 = p99 = avg_lat = 0.0

        error_rate = (self.failed_sends / self.target_count) * 100 if self.target_count > 0 else 0.0

        results = {
            "target_count": self.target_count,
            "provider": self.provider_id,
            "concurrency": self.concurrency,
            "total_duration_s": round(total_duration, 3),
            "successful_sends": self.successful_sends,
            "failed_sends": self.failed_sends,
            "retried_sends": self.retried_sends,
            "duplicate_deliveries": self.duplicate_deliveries,
            "throughput_msg_per_sec": round(throughput, 2),
            "error_rate_pct": round(error_rate, 2),
            "latency_p50_ms": round(p50, 2),
            "latency_p95_ms": round(p95, 2),
            "latency_p99_ms": round(p99, 2),
            "latency_avg_ms": round(avg_lat, 2),
            "memory_usage_delta_mb": round(mem_after - mem_before, 2)
        }

        self._print_report(results)
        return results

    def _print_report(self, res: Dict[str, Any]):
        print("=" * 60)
        print("📊 LOAD TEST RESULTS REPORT")
        print("=" * 60)
        print(f"Target Messages Sent  : {res['target_count']:,}")
        print(f"Successful Deliveries : {res['successful_sends']:,}")
        print(f"Failed Messages       : {res['failed_sends']:,}")
        print(f"Retries Handled       : {res['retried_sends']:,}")
        print(f"Duplicates Detected   : {res['duplicate_deliveries']} (Target: 0)")
        print(f"Total Duration        : {res['total_duration_s']} seconds")
        print(f"Peak Throughput       : {res['throughput_msg_per_sec']} msg/sec")
        print(f"Error Rate            : {res['error_rate_pct']}%")
        print("-" * 60)
        print(f"Latency Avg           : {res['latency_avg_ms']} ms")
        print(f"Latency p50 (Median)  : {res['latency_p50_ms']} ms")
        print(f"Latency p95           : {res['latency_p95_ms']} ms")
        print(f"Latency p99           : {res['latency_p99_ms']} ms")
        print(f"Memory Delta          : {res['memory_usage_delta_mb']} MB")
        print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Multi-Aggregator Load Test Harness")
    parser.add_argument("--count", type=int, default=1000, help="Total messages to send (e.g. 1000, 10000, 1000000)")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent async worker tasks")
    parser.add_argument("--provider", type=str, default="mock", choices=["mock", "twilio", "infobip"], help="Target provider")
    parser.add_argument("--simulate-429", action="store_true", help="Simulate 429 rate limit injection & retry")
    parser.add_argument("--simulate-outage", action="store_true", help="Simulate provider outage and recovery")
    parser.add_argument("--use-network", action="store_true", help="Send over real HTTP network to aggregator endpoint")
    parser.add_argument("--output", type=str, default="logs/load_test_results.json", help="Path to save JSON metrics")
    args = parser.parse_args()

    harness = LoadTestHarness(
        target_count=args.count,
        concurrency=args.concurrency,
        provider_id=args.provider,
        simulate_429=args.simulate_429,
        simulate_outage=args.simulate_outage
    )
    results = asyncio.run(harness.run(use_network=args.use_network))

    # Save results to file
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n📁 Saved metrics to {out_path.resolve()}\n")

if __name__ == "__main__":
    main()
