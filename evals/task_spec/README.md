# Task spec v1

`example.yaml` shows the complete v1 shape. Commands are argument arrays, not shell strings; this module only parses and validates declarations and does not execute setup, agent, or verifier commands. CPU is expressed in millicores and memory in MiB. `reward_range` is an ordered pair describing the verifier's expected score range.

The parser rejects unknown fields and unsupported versions. Benchmark importers and runtime execution are not part of this initial contract.
