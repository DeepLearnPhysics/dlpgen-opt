"""CPU-list parsing shared by host profiles and compute-node runners."""

def parse_cpu_set(value):
    result = []
    for part in value.split(','):
        bounds = [int(v) for v in part.split('-')]
        if len(bounds) not in (1, 2) or min(bounds) < 0 or bounds[-1] < bounds[0]:
            raise ValueError('invalid CPU set')
        result.extend(range(bounds[0], bounds[-1] + 1))
    if len(set(result)) != len(result):
        raise ValueError('duplicate CPU in CPU set')
    return result
