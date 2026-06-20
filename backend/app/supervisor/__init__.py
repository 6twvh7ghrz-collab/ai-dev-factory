"""V2 Supervisor layer — neural hub for Agent orchestration.

V2.0-B1: Task state machine and append-only events.
V2.0-B2a: Worker registry service and capabilities.
V2.0-B2b: Task claim service with lease management and atomic transactions.
V2.0-B2c: Worker heartbeat service for lease renewal.
V2.0-B2d: Lease recovery service for expired lease detection and safe reclamation.

Feature flag: V2_CONTROL_PLANE_ENABLED (default: false).
When disabled, all mutation methods return V2_CONTROL_PLANE_DISABLED.

Available modules:
  - state_machine.py            : TaskStateMachineService
  - task_event_service.py       : TaskEventService
  - worker_registry.py          : WorkerRegistryService
  - task_claim_service.py       : TaskClaimService
  - worker_heartbeat_service.py : WorkerHeartbeatService
  - lease_recovery_service.py   : LeaseRecoveryService
"""
