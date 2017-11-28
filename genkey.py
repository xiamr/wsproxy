#!/usr/bin/env python3

import random

# 0-255

output = []

i = 0
while len(output) < 256:
    r = round(random.uniform(0, 255))
    done = True
    for item in output:
        if item == r:
            done = False
            break
    if done:
        output.append(r)
        i += 1

print(output)
