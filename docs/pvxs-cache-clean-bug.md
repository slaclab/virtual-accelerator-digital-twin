# pvxs Channel Cache GC Bug — Technical Report

**Date**: 2026-09-09
**Affects**: `pvxs` (https://github.com/epics-base/pvxs)
**File**: `src/client.cpp`, function `ContextImpl::cacheClean()`, lines ~1339–1373
**Severity**: Medium — causes unbounded RSS growth over days of continuous operation
**Discovered in**: virtual-accelerator-digital-twin, investigating RSS leak under long-running `p4p` PVA get workloads

---

## Summary

The `pvxs` client channel cache garbage collector (`cacheClean()`) contains a logic bug that renders its two-phase mark/sweep design ineffective. The "mark" phase is dead code — channels are either swept immediately or never swept at all. Under sustained high-frequency `ctx.get()` workloads, this contributes to RSS growth over time because channel entries accumulate in the `chanByName` cache and are never eligible for collection.

---

## Background

### Channel Cache Design

`pvxs::client::ContextImpl` maintains a channel cache in:

```cpp
// clientimpl.h:297
std::map<std::pair<std::string, std::string>, std::shared_ptr<Channel>> chanByName;
```

This is a **strong reference** map. Each `Channel` also holds a `shared_ptr<ContextImpl>` back-reference (line 181), creating an intentional reference cycle. The comment at line 294–295 states:

> strong ref. loop through Channel::context
> explicitly broken by Context::close(), Context::cacheClear(), or ContextImpl::cacheClean()

### Cache Cleaner Timer

A periodic timer fires every 10 seconds (line 57):

```cpp
constexpr timeval channelCacheCleanInterval{10,0};
```

This calls `cacheCleanS()` (line 1375), which calls `cacheClean("", Context::Clean)`.

### Intended Two-Phase GC

The `Channel` struct has a `garbage` flag (line 193 of `clientimpl.h`):

```cpp
bool garbage = false;
```

The intended design is:
1. **Mark phase**: If a channel has `use_count() <= 1` (only `chanByName` holds it), set `garbage = true`
2. **Sweep phase**: On the next timer tick, if `garbage` is still `true`, erase from `chanByName`

This gives channels a grace period — if a new operation reuses the channel between ticks, `Channel::build()` resets `garbage = false` (line 366).

---

## The Bug

In `cacheClean()` (lines 1339–1373):

```cpp
void ContextImpl::cacheClean(const std::string& name, Context::cacheAction action)
{
    auto next(chanByName.begin()),
         end(chanByName.end());

    while(next!=end) {
        auto cur(next++);

        if(!name.empty() && cur->first.first!=name)
            continue;

        else if(action!=Context::Clean || cur->second.use_count()<=1) {
            cur->second->garbage = true;       // LINE 1351: ALWAYS sets garbage = true

            if(action==Context::Clean && !cur->second->garbage) {  // LINE 1353: DEAD CODE
                // mark for next sweep
                log_debug_printf(setup, "Chan GC mark '%s':'%s'\n",
                                 cur->first.first.c_str(), cur->first.second.c_str());

            } else {
                log_debug_printf(setup, "Chan GC sweep '%s':'%s'\n",
                                 cur->first.first.c_str(), cur->first.second.c_str());

                auto trash(std::move(cur->second));

                // explicitly break ref. loop of channel cache
                chanByName.erase(cur);

                if(action==Context::Disconnect) {
                    trash->disconnect(trash);
                }
            }
        }
    }
}
```

### Problem

Line 1351 unconditionally sets `cur->second->garbage = true`.

Line 1353 then tests `!cur->second->garbage` — which is **always false** because line 1351 just set it to `true`.

The "mark for next sweep" branch (lines 1353–1356) is **dead code** that never executes.

### Consequence

When `action == Context::Clean` (the normal timer path):

- If `use_count() <= 1`: the channel is **immediately swept** (skipping the intended mark phase)
- If `use_count() > 1`: the channel is **never touched** — it stays in the cache indefinitely

The two-phase grace period never functions. Channels that are briefly unused between operations get no chance to be reclaimed gracefully, and channels that are continuously referenced (even by transient in-flight operations) are never marked for future collection.

---

## Impact on Long-Running Workloads

In the virtual-accelerator-digital-twin, the `take_snapshot()` function calls `pvua_context.get(pv)` for each PV in a tight loop:

```python
# lume_pva/runner.py (patched), take_snapshot()
for pv in self.snapshot_pvs:
    new_values[self.pv_to_var[pv]] = {
        "value": self.pvua_context.get(pv),
        "ts": time.time(),
    }
```

Each `ctx.get()` creates a `GPROp` (in `clientget.cpp`) that holds a `shared_ptr<Channel>`. While the operation is in-flight, `Channel::use_count() >= 2` (one from `chanByName`, one from `GPROp::chan`).

The `GPROp` destruction is asynchronous — it's dispatched to the pvxs event loop via `loop.tryInvoke()` in the custom deleter (clientget.cpp ~645–656). Under high-frequency get() calls, the event loop may have a backlog of pending GPROp destructions, keeping `use_count() > 1` across multiple `cacheClean` ticks.

Because the mark phase is dead code, these channels are never marked as `garbage` — they survive indefinitely in the cache. Over days of operation, the cache accumulates stale entries that hold Connection objects, type registries, and other C++ state, contributing to steady RSS growth.

---

## Proposed Fix

Move the `garbage = true` assignment into the mark branch:

```cpp
void ContextImpl::cacheClean(const std::string& name, Context::cacheAction action)
{
    auto next(chanByName.begin()),
         end(chanByName.end());

    while(next!=end) {
        auto cur(next++);

        if(!name.empty() && cur->first.first!=name)
            continue;

        else if(action!=Context::Clean || cur->second.use_count()<=1) {

            if(action==Context::Clean && !cur->second->garbage) {
                // mark for next sweep
                cur->second->garbage = true;
                log_debug_printf(setup, "Chan GC mark '%s':'%s'\n",
                                 cur->first.first.c_str(), cur->first.second.c_str());

            } else {
                log_debug_printf(setup, "Chan GC sweep '%s':'%s'\n",
                                 cur->first.first.c_str(), cur->first.second.c_str());

                auto trash(std::move(cur->second));

                // explicitly break ref. loop of channel cache
                chanByName.erase(cur);

                if(action==Context::Disconnect) {
                    trash->disconnect(trash);
                }
            }
        }
    }
}
```

This restores the intended two-phase behavior:
1. First tick with `use_count() <= 1`: set `garbage = true` (mark)
2. Second tick with `garbage == true`: erase from `chanByName` (sweep)
3. If a new operation reuses the channel between ticks: `Channel::build()` sets `garbage = false`, saving it from collection

---

## Additional Contributing Factors

### 1. `use_count()` Stays Elevated Under Load

Even with the fix, if `GPROp` destructions are delayed by event loop backlog, `use_count()` may remain > 1 across ticks and channels will never enter the mark phase. A complementary fix would be to also mark channels when `use_count() > 1` but no operations are logically pending (i.e., `pending.empty() && opByIOID.empty()`).

### 2. `rxRegistry` Type Cache Per Connection

Each `Connection` has an `rxRegistry` (type description cache) that grows with each unique type received from the server. Over days, especially if the server restarts with slightly different type layouts, this can accumulate stale entries.

### 3. `chanByCID` Map Uses `weak_ptr` But Is Never Compacted

Dead `weak_ptr` entries in `chanByCID` are checked lazily (on lookup). The map itself never shrinks, so the map's internal tree nodes accumulate over time.

---

## Verification

### Test Suite

The file `tests/test_pva_get_leak.py` exercises the p4p/pvxs get path in isolation (no docker, no bmad) with:
- 50 in-process PVs served via `p4p.server.thread.SharedPV`
- Repeated gets, context open/close cycles, soak test with RSS timeline
- Valgrind memcheck integration via `scripts/run_pva_leak_valgrind.sh`

### Valgrind Results

Valgrind memcheck found **zero definitely-lost or indirectly-lost bytes** from p4p/pvxs. The 3 "possibly lost" reports are all false positives:
- 400 bytes: `pthread_create` → `epicsThreadOnce` → `pvxs::impl::mapInit()` (one-time static init)
- 2,495 bytes: Python `unicode_join` during module import
- 169,228 bytes: Python `unicode_join` during module import

### RSS Measurements (Without Valgrind)

| Test | Iterations | RSS delta |
|---|---|---|
| repeated_gets (50 PVs, 50K iters) | 2.5M get calls | +0.1 MB |
| soak (50 PVs, 5 min) | 135K get calls | +0.0 MB |
| context_open_close (500 cycles) | 25K get calls | +5.9 MB (glibc arena fragmentation) |

The in-process test doesn't trigger the bug because the test creates short-lived contexts or runs all gets synchronously on a single context with low concurrency. The bug manifests under production conditions where the event loop has backlog pressure from the bmad model computation.

---

## Recommendation

1. **File issue** on https://github.com/epics-base/pvxs with the proposed fix
2. **Workaround**: periodically call `ctx.cacheClear()` from the runner to force-sweep the channel cache (the `cacheClear` path calls `cacheClean` twice with action=Clean, but due to the bug this still immediately sweeps `use_count()<=1` channels)
3. **Monitor**: add a Prometheus gauge for `chanByName.size()` to track cache growth over time
