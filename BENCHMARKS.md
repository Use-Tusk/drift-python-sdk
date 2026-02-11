# Benchmarking guide for Tusk Drift Python SDK

## Overview

The benchmarking methodology is the same across all our SDKs.
It piggy backs off the end to end tests, so that test cases are automatically
discovered, and we don't have to setup multiple apps in multiple different
places to run tests.

The simplest way to get started it simply
```
./run-all-benchmarks.sh
```
or to run a single (or a few) instrumentations,
```
./run-all-benchmarks.sh -f flask,fastapi         
```

Within each instrumentation's e2e tests, you can also run it as such
```
BENCHMARKS=1 ./drift/instrumentation/aiohttp/e2e-tests/run.sh
```
but it is not as convenient. The run-all script is essentially just automating
this for you (there's some other settings it sets automatically)

## Methodology

Benchmarks are run with a (by default) 3 second warm up, and 10 seconds of
actual measurement. This is very important especially for tests that make http
or filesystem queries, since the warm up also warms up all system caches (think
ARP, DNS, buffers, etc.). Disabling warm up *will* produce nonsensical results.

The benchmarking process itself is simple. We start the app exactly as how the
e2e tests would, but the make_request helper function used by the e2e tests have
been hijacked to run continuously instead of returning after a single call.
We then measure the ops/second achieved during our 10s test interval, and print
out the results. We do this first for the SDK disabled case, and then again for
the SDK enabled case. The SDK is enabled/disabled through the standard
`TUSK_DRIFT_MODE` envvar.

## Implementing benchmarks

If you've read through the above you will realise adding benchmarks is really
easy!
Simply add a new endpoint to an existing e2e test, or add a new e2e test
entirely in the same format -- the run-all script auto discovers based on the
run.sh script in each e2e test folder.
