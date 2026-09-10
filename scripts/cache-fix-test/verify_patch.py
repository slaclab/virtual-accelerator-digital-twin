"""Verify that p4p is using a patched pvxslibs (cacheClean fix)."""
import os
import sys
import subprocess

def main():
    import pvxslibs
    libdir = os.path.join(os.path.dirname(pvxslibs.__file__), "lib")
    print(f"pvxslibs dir: {libdir}")

    # Find libpvxs.so
    pvxs_so = None
    for f in sorted(os.listdir(libdir)):
        if f.startswith("libpvxs.so"):
            print(f"  {f}")
            if pvxs_so is None and not f.endswith(".so"):
                pvxs_so = os.path.join(libdir, f)
    if pvxs_so is None:
        pvxs_so = os.path.join(libdir, "libpvxs.so")

    print(f"\nChecking: {pvxs_so}")

    # strings check for the debug log near the patch site
    result = subprocess.run(
        ["strings", pvxs_so], capture_output=True, text=True
    )
    gc_lines = [l for l in result.stdout.splitlines() if "GC mark" in l or "GC sweep" in l or "Chan GC" in l]
    print(f"\nGC-related strings in libpvxs.so ({len(gc_lines)} found):")
    for l in gc_lines:
        print(f"  {l}")

    # ldd on p4p._p4p
    import p4p._p4p
    p4p_so = p4p._p4p.__file__
    print(f"\np4p native module: {p4p_so}")
    result = subprocess.run(["ldd", p4p_so], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if "pvxs" in line or "Com" in line or "epicscorelibs" in line:
            print(f"  {line.strip()}")

    # Quick import+close test (should not segfault)
    print("\nSmoke test: import p4p, create Context, close...")
    from p4p.client.thread import Context
    ctx = Context("pva")
    ctx.close()
    print("OK — no crash")

if __name__ == "__main__":
    main()
