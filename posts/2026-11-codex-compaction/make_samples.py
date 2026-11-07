#!/usr/bin/env python3
"""Generate four synthetic C++ source files (f1.cpp to f4.cpp) for the compaction experiment.

Deterministic: the same arguments always produce byte-identical files. The code is invented filler (it compiles
in spirit, not in practice); its only job is to be about 55,000 tokens in total (estimated at 4 characters per
token), with f4.cpp about half of it. Each file ends with a function whose name is easy to recognize.

Usage: make_samples.py [OUT_DIR]   (default: current directory)
"""
import random
import sys
from pathlib import Path

# (file name, target size in characters, last function name)
FILES = [
    ('f1.cpp', 36000, 'InventoryLedger::ReconcileAll'),
    ('f2.cpp', 36000, 'SessionRouter::RegisterRoutes'),
    ('f3.cpp', 36000, 'PipelineWiring::Register'),
    ('f4.cpp', 112000, 'RegisterBillingRoutes'),
]
NOUNS = ['Order', 'Ticket', 'Shard', 'Lease', 'Quota', 'Cursor', 'Bucket', 'Window', 'Token', 'Batch', 'Index', 'Frame']
VERBS = ['Load', 'Store', 'Merge', 'Split', 'Verify', 'Rotate', 'Flush', 'Resolve', 'Encode', 'Decode', 'Audit', 'Seal']
TYPES = ['int64_t', 'uint32_t', 'double', 'size_t']


def function(rng, owner, index):
    name = f'{rng.choice(VERBS)}{rng.choice(NOUNS)}{index}'
    a, b = rng.sample(['count', 'limit', 'offset', 'weight', 'budget', 'epoch'], 2)
    t = rng.choice(TYPES)
    lines = [f'{t} {owner}::{name}({t} {a}, {t} {b}) {{',
             f'    {t} total = 0;',
             f'    for (size_t i = 0; i < entries_.size(); ++i) {{',
             f'        const auto& e = entries_[i];',
             f'        if (e.{a} > {b}) {{',
             f'            total += e.{a} - {b};',
             f'        }} else if (e.{b} % {rng.randint(3, 97)} == {rng.randint(0, 2)}) {{',
             f'            total -= static_cast<{t}>(e.{b} / {rng.randint(2, 9)});',
             f'        }}']
    for k in range(rng.randint(2, 6)):
        lines += [f'        if (total > {rng.randint(100, 99999)} * (i + {k})) {{',
                  f'            log_.Warn("{name}: threshold {k} exceeded at " + std::to_string(i));',
                  f'            total = total % {rng.randint(1000, 9999)};',
                  '        }']
    lines += ['    }', f'    cache_[{index}] = total;', '    return total;', '}', '']
    return '\n'.join(lines) + '\n'


def build(name, size, last, seed):
    rng = random.Random(seed)
    owner = last.split('::')[0] if '::' in last else 'Service'
    parts = [f'// {name}: synthetic filler for the compaction experiment. Not real code.\n',
             '#include <cstdint>\n#include <string>\n#include <vector>\n\n',
             f'namespace sample_{name[1]} {{\n\n']
    n = 0
    total = sum(len(p) for p in parts)
    while total < size:
        chunk = function(rng, owner, n)
        parts.append(chunk)
        total += len(chunk)
        n += 1
    tail = last if '::' in last else last
    parts.append(f'void {tail}(Router& router) {{\n    router.Add("/{name[:2]}", &{owner}::Handle);\n}}\n\n}}  // namespace\n')
    return ''.join(parts)


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
    out.mkdir(parents=True, exist_ok=True)
    for i, (name, size, last) in enumerate(FILES):
        text = build(name, size, last, seed=7000 + i)
        (out / name).write_text(text, encoding='utf-8')
        print(f'{name}: {len(text.splitlines())} lines, {len(text)} chars, ~{len(text) // 4} tokens, last function {last}')


if __name__ == '__main__':
    main()
