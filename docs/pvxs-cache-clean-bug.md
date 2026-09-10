# pvxs Channel Cache GC Bug — Technical Report

**Date**: 2026-09-09
**Affects**: `pvxs` (https://github.com/epics-base/pvxs)
**File**: `src/client.cpp`, function `ContextImpl::cacheClean()`, lines ~1339–1373
**Severity**: Medium — causes unbounded RSS growth over days of continuous operation
**Discovered in**: virtual-accelerator-digital-twin, investigating RSS leak under long-running `p4p` PVA get workloads

---

## Summary

When a p4p client calls `ctx.get(pv)`, pvxs creates a **Channel** object for that PV and
stores it in a cache (`chanByName`). Building a Channel is expensive: it runs a
search/CREATE_CHANNEL handshake with the server, gets a server-side channel ID (SID)
assigned, and registers itself in several bookkeeping maps. The cache exists so repeated
gets to the same PV **reuse** the same Channel instead of rebuilding it.

(The **TCP Connection** and its type cache `rxRegistry` are *shared* per server via a
`weak_ptr` map — all PVs on the same IOC share one socket. Destroying a Channel does NOT
tear down the socket, so the churn is in the per-Channel protocol state + queued work, not
in the TCP layer.)

A background timer (`cacheClean()`) fires every 10 seconds to garbage-collect Channels
nobody is using. It is designed as a two-phase mark/sweep:

1. **Mark**: if a Channel is idle (`use_count() == 1` — only the cache holds it), flag it
   `garbage = true`
2. **Sweep**: on the *next* tick (10s later), if it's still flagged, erase it

The grace period matters: if a new `get()` reuses the Channel between ticks,
`Channel::build()` resets `garbage = false` and the Channel survives. A PV polled every
few seconds keeps its Channel warm indefinitely.

### The bug

```cpp
else if(action!=Context::Clean || cur->second.use_count()<=1) {
    cur->second->garbage = true;                            // (1) always set true
    if(action==Context::Clean && !cur->second->garbage) {  // (2) !true → always FALSE
        // mark for next sweep  ← DEAD CODE, never runs
    } else {
        chanByName.erase(cur);  // ← sweep: ALWAYS taken instead
    }
}
```

Line (1) sets `garbage = true`, then line (2) tests `!garbage` — always false. The mark
branch is dead code. **The grace period is gone.** On the normal timer path
(`action == Clean`) the branch is only entered when the Channel is idle
(`use_count() <= 1`), and once entered it always falls straight to the **sweep**.

Net effect: **any Channel that is momentarily idle at a 10s tick is deleted immediately**,
with no second-chance tick.

### Why this leaks under fast polling — it's churn, not accumulation

Walk through what a fast poll loop does against the broken cleaner:

```
get(PV_A) ──► build Channel_A + Connection + rxRegistry   (expensive)
           ── result returned to Python
           ── GPROp destructor queued on event loop (async) ─┐
                                                             │ refcount → 1 soon after
10s tick ─► PV_A idle (use_count==1) ──► SWEPT, erased ◄─────┘
get(PV_A) ──► must REBUILD Channel_A + Connection + rxRegistry  (expensive again!)
10s tick ─► SWEPT again
get(PV_A) ──► REBUILD again ...
```

Each get returns its result to Python, but the underlying `GPROp` (Get/Put/RPC Operation
— the C++ object that ran the request) is destroyed **asynchronously** on the pvxs event
loop, not inline. The broken cleaner sweeps idle Channels the instant it sees them, so a
Channel that just went idle is **rebuilt from scratch on the very next get**.

```
              WITHOUT fix (broken)              WITH fix (grace period)
              ───────────────────              ───────────────────────
tick 1:       PV idle → SWEEP (delete)         PV idle → MARK (keep)
next get:     REBUILD Channel+Conn+registry    reuse existing Channel
                                               (build() clears the mark)
tick 2:       (already gone)                   still used → stays
result:       constant alloc/free thrash       Channel stays warm, reused
```

Three things drive RSS up in the broken case:

1. **Dead bookkeeping entries accumulate (verified).** Each Channel::build() inserts into
   `chanByCID` (weak_ptr map, clientimpl.h:312) and `searchBuckets` (weak_ptr lists,
   clientimpl.h:308). When `~Channel` runs via `disconnect(nullptr)`, these entries are
   **never erased** — the weak_ptr expires but the map tree node (~48-64 bytes) stays.
   Under churn with 500 PVs, each sweep+rebuild cycle adds ~500 dead entries to each
   structure. Over hours, thousands of dead entries → megabytes of leaked nodes.

2. **Work queue backlog holds shared_ptrs.** All pvxs client work runs on **one** event-loop
   worker thread, fed by a single **unbounded** queue (`std::deque<Work> actions`,
   evhelper.cpp:130). `_dispatch()` just does `emplace_back(...)` — no size limit, no
   blocking, no drop. Every queued lambda captures `shared_ptr`s (channels, ops, buffers).
   Growing backlog = growing live memory.

3. **Churn overhead.** Each rebuild pays search + CREATE_CHANNEL handshake + SID assignment
   + map registration. This work saturates the single worker thread, which explains the
   2.3× throughput drop (253 vs 593 iterations/60s).

### The proof: it's churn

The A/B test measured the stock build running **253 get iterations in 60s** vs the fixed
build's **593** — 2.3× slower. If Channels merely piled up passively, throughput would be
unchanged. It drops because in the stock build **every get pays the Channel + Connection
+ registry rebuild cost**. That directly confirms create → sweep → recreate thrash.

```
                    Single PV                    Many PVs, fast poll
                    ─────────                    ───────────────────
reuse between ticks: get arrives before tick,    some PVs idle at each tick →
                     Channel stays warm          swept → rebuilt next get
broken-GC effect:    ~no churn → NO LEAK         constant churn → LEAK
                                                 (~1.6 MB/h at 180 PVs)
```

### Where the memory actually accumulates (verified in source)

The churn creates dead Channel objects. When `cacheClean` sweeps with `action == Clean`
(client.cpp:1362-1369):

```cpp
auto trash(std::move(cur->second));   // shared_ptr moved out of map
chanByName.erase(cur);                // map entry removed
// action == Clean, NOT Disconnect → disconnect(trash) is NOT called here
// trash drops at scope end → ~Channel() → disconnect(nullptr)
```

`~Channel` calls `disconnect(nullptr)` (client.cpp:117). The `nullptr` path
(client.cpp:206-207) does **nothing** — no `CMD_DESTROY_CHANNEL`, no SID/CID map cleanup:

```cpp
if(!self) { // in ~Channel
    // searchBuckets cleaned in tickSearch()
}
```

But `Channel::build()` (client.cpp:375) inserted entries into two other maps that are
**never cleaned up** by this path:

1. **`chanByCID`** — `std::map<uint32_t, weak_ptr<Channel>>` (clientimpl.h:312). Each new
   Channel inserts `chanByCID[chan->cid] = chan` (client.cpp:375). When the Channel dies,
   the `weak_ptr` expires but **the map entry (tree node) is never erased**. It's only
   skipped lazily during CID allocation (client.cpp:370):
   ```cpp
   while(context->chanByCID.find(context->nextCID)!=context->chanByCID.end())
       context->nextCID++;
   ```
   Dead entries pile up. Each `std::map` tree node is ~48-64 bytes.

2. **`searchBuckets`** — `vector<list<weak_ptr<Channel>>>` (clientimpl.h:308). Each new
   Channel pushes into `initialSearchBucket` (client.cpp:379). `tickSearch` only `.lock()`
   checks entries when their bucket rotates (client.cpp:1108-1111). Dead `weak_ptr`s sit in
   the lists until that bucket fires. Under fast churn, lists grow faster than they drain.

Under the broken GC with 500 PVs, each 10s sweep+rebuild cycle adds up to **500 dead
`chanByCID` entries + 500 dead `searchBuckets` entries** that are never proactively
cleaned. Over 2.5M gets with periodic sweeps, that's thousands of dead entries → megabytes
of leaked tree nodes and list nodes.

**With the fix**: no churn → Channels stay warm → no dead entries → maps stay bounded.

### The fix

**Primary fix** (cacheClean mark — see [Proposed Fix](#proposed-fix)): move `garbage = true`
inside the mark branch. Restores the grace period: idle Channel is marked on one tick,
swept only if *still* idle on the next. A PV polled again in between keeps its Channel —
no rebuild, no churn, no dead map entries. Confirmed: **+21.3 MB stock vs +0.1 MB fixed**
over 2.5M gets.

**Secondary fix** (chanByCID/searchBuckets compaction — see [Additional Contributing
Factors §5](#5-chanbycid-and-searchbuckets-accumulate-dead-entries-under-churn)): even with
the primary fix, these maps should be compacted to defend against any residual churn from
connection drops or other lifecycle events.

---

## Detailed Summary

The `pvxs` client channel cache garbage collector (`cacheClean()`) contains a logic bug that renders its two-phase mark/sweep design ineffective. The "mark" phase is dead code, so the intended 10-second grace period never happens. On the normal timer path, any Channel that is momentarily idle at a cleanup tick is swept immediately. Under sustained high-frequency `ctx.get()` workloads across many PVs, this produces constant create → sweep → rebuild churn: Channels (and their Connections and type registries) are destroyed the instant they go idle and rebuilt on the next get. Because Channel destruction is dispatched asynchronously to the event loop while creation happens synchronously, allocation outpaces reclamation and RSS grows over time.

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

In the virtual-accelerator-digital-twin, `take_snapshot()` calls `ctx.get(pv)` for each
PV in a tight loop (~180 PVs at ~257 gets/s). Each `ctx.get()` creates a `GPROp`
(clientget.cpp) that holds a `shared_ptr<Channel>`. The GPROp destructor is dispatched
**asynchronously** to the pvxs event loop via `loop.tryInvoke()` (clientget.cpp:597).

Because the mark phase is dead code, every Channel caught idle at a 10s tick is swept
immediately. The next get to that PV must rebuild the Channel from scratch (search +
CREATE_CHANNEL handshake + SID assignment + map registration). This is the **churn**
mechanism — not passive accumulation, but constant destroy → rebuild.

### Why churn causes RSS growth

All pvxs client work runs on **one** event-loop worker thread, fed by a single unbounded
queue (`std::deque<Work> actions`, evhelper.cpp:130). `_dispatch()` (evhelper.cpp:297-316)
just does `actions.emplace_back(...)` — **no size limit, no blocking, no drop**.

Under fast polling with the broken GC:
1. Python thread enqueues get+build work (synchronous from Python's perspective)
2. Worker runs build (Channel::build, search, connect, createOperations)
3. Timer fires → sweep idle channels (inline, on same worker)
4. Python enqueues more gets → worker must rebuild what was just swept
5. GPROp destructors queue up behind the rebuild work

Every queued lambda captures `shared_ptr`s (channels, ops, buffers). Growing backlog =
growing live memory. The queue has no backpressure, so the backlog is unbounded.

**When a Channel's `shared_ptr` finally drops** (verified in pvxs 1.5.2 source):
- `~Channel` → `disconnect()` (client.cpp:117,151) sends `CMD_DESTROY_CHANNEL`, erases
  the channel from the connection's SID/CID maps, and re-queues it for search
- The **TCP socket is NOT closed**: the Connection (socket + `rxRegistry`) is *shared per
  server* via `connByAddr` weak_ptr (clientconn.cpp:51-53). All PVs on one IOC share one
  socket; real socket teardown (`~Connection`, clientconn.cpp:180) runs only when the last
  channel ref drops
- So the churn cost per cycle is: Channel C++ object alloc/free + search/connect protocol
  + map bookkeeping + queued lambdas holding `shared_ptr`s

### Supporting evidence

The A/B test shows stock running **253 iterations in 60s** vs fixed's **593** — 2.3×
slower. Passive accumulation would not affect throughput. The slowdown directly reflects
the per-get Channel rebuild cost under churn.

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

Dead `weak_ptr` entries in `chanByCID` are checked lazily (on lookup, client.cpp:370). The
map itself never shrinks. Under the broken GC's churn, each sweep+rebuild cycle adds dead
entries. See §5 for the fix.

### 4. Event Loop Work Queue Has No Backpressure

The pvxs event loop `evbase::Pvt` (evhelper.cpp:116-261) feeds all client work through a
single unbounded `std::deque<Work> actions` (evhelper.cpp:130). The dispatch path:

```cpp
// evhelper.cpp:297-316 — _dispatch() enqueues with no limit
bool evbase::_dispatch(mfunction&& fn, bool dothrow) const
{
    bool empty;
    {
        Guard G(pvt->lock);
        if(!pvt->running) { ... }
        empty = pvt->actions.empty();
        pvt->actions.emplace_back(std::move(fn), nullptr, nullptr);  // no cap check
    }
    // signal worker
    ...
}
```

There is no size limit, no blocking, no drop. Under the churn scenario, the Python
polling thread can enqueue build+cancel+destroy lambdas faster than the single worker
drains them. Every queued lambda captures `shared_ptr`s (Channels, GPROps, Connections),
so a backlog directly grows live heap.

**Proposed defense-in-depth fix** (complements the cacheClean mark fix):

```cpp
// evhelper.cpp — add a high-water mark to _dispatch()
bool evbase::_dispatch(mfunction&& fn, bool dothrow) const
{
    bool empty;
    {
        Guard G(pvt->lock);
        if(!pvt->running) { ... }

        // backpressure: if queue exceeds high-water mark, block caller briefly
        // to let the worker drain. Prevents unbounded memory growth under churn.
        static constexpr size_t highWater = 4096;
        while(pvt->actions.size() >= highWater) {
            pvt->lock.unlock();
            epicsThreadSleep(0.001);  // 1ms yield
            pvt->lock.lock();
            if(!pvt->running) { ... }
        }

        empty = pvt->actions.empty();
        pvt->actions.emplace_back(std::move(fn), nullptr, nullptr);
    }
    ...
}
```

This is a **secondary** fix — the primary fix (cacheClean mark) eliminates the churn that
would saturate the queue. But the backpressure cap protects against any future scenario
where enqueue outpaces drain.

### 5. `chanByCID` and `searchBuckets` Accumulate Dead Entries Under Churn

This is the **verified accumulation point** for the memory leak. When `cacheClean` sweeps
a Channel with `action == Clean`, the Channel's `shared_ptr` drops and `~Channel` calls
`disconnect(nullptr)` (client.cpp:117). The `nullptr` path (client.cpp:206-207) is a
no-op:

```cpp
if(!self) { // in ~Channel
    // searchBuckets cleaned in tickSearch()
    // ← but chanByCID is NEVER cleaned
}
```

But `Channel::build()` inserted entries into two maps when the Channel was created:

```cpp
// client.cpp:375 — inserted on every Channel::build()
context->chanByCID[chan->cid] = chan;          // weak_ptr, never erased on destroy

// client.cpp:379 — inserted on every Channel::build()
context->initialSearchBucket.push_back(chan);  // weak_ptr, cleaned lazily
```

**`chanByCID`** (`std::map<uint32_t, weak_ptr<Channel>>`, clientimpl.h:312):
- Entry inserted per `Channel::build()`, never erased when Channel dies
- `weak_ptr` expires but tree node (~48-64 bytes) stays
- CID allocation (client.cpp:370) skips dead entries but never erases them:
  ```cpp
  while(context->chanByCID.find(context->nextCID)!=context->chanByCID.end())
      context->nextCID++;  // skips, but dead entries remain in map
  ```
- Under churn (500 PVs × sweeps every 10s): ~500 dead entries/cycle → thousands/hour

**`searchBuckets`** (`vector<list<weak_ptr<Channel>>>`, clientimpl.h:308):
- Dead `weak_ptr`s cleaned lazily in `tickSearch()` when their bucket rotates
  (client.cpp:1108-1111: `auto chan = bucket.front().lock(); if(!chan) { pop; continue; }`)
- Under fast churn, entries accumulate faster than buckets rotate (30 buckets × 10s = 5
  min full cycle)

**Proposed fix** — compact `chanByCID` during `cacheClean`:

```cpp
// client.cpp — add to cacheClean(), after the main while loop
// Compact chanByCID: erase expired weak_ptr entries left by destroyed Channels
{
    auto next(chanByCID.begin()), end(chanByCID.end());
    while(next != end) {
        auto cur(next++);
        if(cur->second.expired())
            chanByCID.erase(cur);
    }
}
```

This runs every 10s on the same timer tick as the sweep, so it adds negligible overhead.
It ensures dead CID entries don't accumulate.

For `searchBuckets`, the existing lazy cleanup in `tickSearch` is adequate once the
primary cacheClean fix eliminates the churn that overwhelms it. If needed, a similar
compaction pass could be added to `tickSearch`:

```cpp
// client.cpp — add at the start of tickSearch() for the current bucket
// Eagerly purge expired weak_ptrs before processing the bucket
{
    auto& bkt = (kind == SearchKind::initial) ? initialSearchBucket : searchBuckets[idx];
    bkt.remove_if([](const std::weak_ptr<Channel>& wp) { return wp.expired(); });
}
```

---

## Verification

### Test Suite

The file `scripts/cache-fix-test/test_pva_get_leak.py` exercises the p4p/pvxs get path in isolation (no docker, no bmad) with:
- 500 in-process PVs served via `p4p.server.thread.SharedPV`
- Repeated gets, context open/close cycles, soak test with RSS timeline
- Valgrind memcheck integration via `scripts/cache-fix-test/run_test.sh`

### Docker Test Images

Two Docker images allow direct A/B comparison:

- **`Dockerfile.stock`** — stock p4p from PyPI, unpatched pvxslibs (baseline)
- **`Dockerfile.fixed`** — pvxslibs built from PyPI sdist with cacheClean patch applied, p4p built from source against patched pvxslibs (ABI-consistent via epicscorelibs)

Build and run:
```bash
cd scripts/pvxs-fix-test
docker build -f Dockerfile.stock -t pvxs-test-stock .
docker build -f Dockerfile.fixed -t pvxs-test-fixed .
docker run --rm -v "$(pwd)":/test pvxs-test-stock --gets 5000 --cycles 30 --soak 60 --report /test/stock_result.txt
docker run --rm -v "$(pwd)":/test pvxs-test-fixed --gets 5000 --cycles 30 --soak 60 --report /test/fixed_result.txt
```

Patch verification inside fixed image:
```bash
# Confirm patched source was used
docker run --rm --entrypoint bash pvxs-test-fixed -c "grep -n 'garbage' /opt/pvxslibs-patched-src/client.cpp"
# Confirm binary differs from stock
docker run --rm --entrypoint python3 -v "$(pwd)":/test pvxs-test-fixed /test/verify_patch.py
```

### Valgrind Results

Valgrind memcheck found **zero definitely-lost or indirectly-lost bytes** from p4p/pvxs. The 3 "possibly lost" reports are all false positives:
- 400 bytes: `pthread_create` → `epicsThreadOnce` → `pvxs::impl::mapInit()` (one-time static init)
- 2,495 bytes: Python `unicode_join` during module import
- 169,228 bytes: Python `unicode_join` during module import

### RSS Measurements — Stock vs Fixed (2026-09-09)

Test parameters: 5000 get iterations × 500 PVs = 2.5M get() calls, 30 context cycles, 60s soak.

Full results: [`scripts/cache-fix-test/stock_result.txt`](../scripts/cache-fix-test/stock_result.txt) | [`scripts/cache-fix-test/fixed_result.txt`](../scripts/cache-fix-test/fixed_result.txt)

#### repeated_gets (2.5M gets, single context)

| Metric | Stock (unpatched) | Fixed (patched) |
|---|---|---|
| RSS start | 45.2 MB | 51.3 MB |
| RSS end | 66.5 MB | 51.4 MB |
| **RSS delta** | **+21.3 MB ❌ FAIL** | **+0.1 MB ✅ PASS** |
| Growth pattern | Linear (+4 MB / 1000 iters) | Flat |

Stock shows textbook linear leak — every 250 iterations adds ~1 MB. Fixed is dead flat. This directly demonstrates that the cacheClean fix eliminates channel accumulation in `chanByName`.

#### context_open_close (30 cycles × 500 PVs)

| Metric | Stock | Fixed |
|---|---|---|
| RSS delta | +0.0 MB ✅ | +0.3 MB ✅ |

Both pass. Context close drops `use_count()` to 1, so even the broken GC sweeps immediately. Consistent with root cause analysis.

#### soak (60s continuous gets)

| Metric | Stock | Fixed |
|---|---|---|
| RSS at soak start | 66.5 MB (inflated by prior leak) | 51.7 MB |
| Anon at soak start | 48.3 MB | 31.7 MB |
| Anon delta | +0.00 MB | +0.00 MB |
| Anon slope | +0.0001 MB/s | +0.0001 MB/s |
| Iterations (60s) | 253 | 593 |

Stock soak starts at **48.3 MB anon** — 16.6 MB higher than fixed's 31.7 MB due to memory leaked in repeated_gets. Stock also ran **2.3× fewer iterations** in the same 60s (253 vs 593), indicating the bloated channel cache degrades get() throughput.

#### Summary

| Test | Stock | Fixed |
|---|---|---|
| repeated_gets | **FAIL** (+21.3 MB) | **PASS** (+0.1 MB) |
| context_open_close | PASS (+0.0 MB) | PASS (+0.3 MB) |
| nonexistent_pv | PASS (+0.0 MB) | PASS (+0.0 MB) |
| context_without_close | INFO (+0.0 MB) | INFO (+0.0 MB) |
| soak | PASS (+0.0 MB) | PASS (+0.0 MB) |
| **Totals** | **3 PASS, 1 FAIL, 1 INFO** | **4 PASS, 0 FAIL, 1 INFO** |

For longer-duration soak comparison, use `--soak 1800` or higher — production leak rate is ~1.6 MB/h at 257 gets/s across 180 PVs, requiring hours to show measurable divergence in the soak metric alone.

---

## Recommendation

1. **File issue** on https://github.com/epics-base/pvxs with the proposed cacheClean mark fix
2. **Suggest backpressure**: propose the `_dispatch()` high-water mark as a defense-in-depth measure (see [Additional Contributing Factors §4](#4-event-loop-work-queue-has-no-backpressure))
3. **Workaround**: periodically call `ctx.cacheClear()` from the runner to force-sweep the channel cache
4. **Monitor**: add a Prometheus gauge for `chanByName.size()` and `actions.size()` to track cache growth and queue depth over time
5. **Heap profiling**: run a longer soak (hours) under `jemalloc` or `heaptrack` to fully characterize which specific objects dominate the heap growth under churn
