# Cloud Storage persistence configuration

Captured on 2026-11-12, right after the 48-hour window, with
`gcloud beta run instances describe kairo --region us-west1 --format=yaml` and
`gcloud storage buckets describe gs://kairo-dk2026-kairo-state`. Environment variable values are
left out (one holds the laptop's tailnet address); secret volumes are shown by name only.

## Cloud Run Instance `kairo` (excerpt)

```yaml
spec:
  containers:
  - image: us-west1-docker.pkg.dev/kairo-dk2026/kairo/kairo@sha256:79e3173a25237ca384739389d7d5de3b67e1585b4fbcbd8fbd9b524b4650bcad
    resources:
      limits:
        cpu: '1'
        memory: 2Gi
    volumeMounts:
    - mountPath: /mnt/agent-state       # durable state (Cloud Storage volume)
      name: state
    - mountPath: /secrets/env           # Secret Manager, read-only
    - mountPath: /secrets/chatgpt       # Secret Manager, read-only
    - mountPath: /secrets/approval      # Secret Manager, read-only
  restartPolicy: Always
  serviceAccountName: kairo-agent@kairo-dk2026.iam.gserviceaccount.com
  volumes:
  - csi:
      driver: gcsfuse.run.googleapis.com
      volumeAttributes:
        bucketName: kairo-dk2026-kairo-state
        mountOptions: uid=1000,gid=1000,file-mode=600,dir-mode=700
    name: state
status:
  urls:
  - https://kairo-270801978750.us-west1.run.app
```

The same settings are in `deploy/deploy.sh` (`--add-volume name=state,type=cloud-storage,...` and
`--add-volume-mount volume=state,mount-path=/mnt/agent-state`).

## Bucket `gs://kairo-dk2026-kairo-state`

```yaml
location: US-WEST1
public_access_prevention: enforced
uniform_bucket_level_access: true
soft_delete_policy:
  retentionDurationSeconds: '604800'   # 7 days
```

## How state uses the volume

- **Snapshots:** `snapshots/snap-<UTC>.tar.gz` holds Kairo state and the Hermes home: memory, sessions,
  cron jobs and execution history, ledger, outbox. It is written every 15 minutes and again before
  each planned restart (`backup_ok`, 199 times in the window).
- **Restore:** each start restores the newest snapshot (`restore_ok`, 16/16 starts). The restored
  file names are listed per restart in `README.md`.
- **Lifecycle logs:** `lifecycle/events.jsonl` and `lifecycle/heartbeat.jsonl` are appended directly
  on the volume. Exported copies are in `logs/bucket_events.jsonl` and `logs/bucket_heartbeat.jsonl`.
- **Flags:** `flags/` holds operator switches read by the supervisor without a redeploy (for example
  `planned_every_h`).
