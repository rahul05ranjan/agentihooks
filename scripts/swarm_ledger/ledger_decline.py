"""agentihooks ledger decline: the operator declined a swarm or small ledger for this session."""

import json


def main():
    print(json.dumps({"declined": True}))
    return 0
