# WyseARR Deployment Lessons Learned

## Purpose

Record deployment lessons from WyseARR stack creation and recovery so future rebuilds, upgrades, and troubleshooting avoid repeating known failure modes.

---

## 1. ARR Image and Version Compatibility Must Be Verified Before Deployment

### Event
An image was selected that did not match the expected automation/API workflow.

Whisparr deployed with v2 behavior while the rest of the ARR stack followed Sonarr/Radarr/Lidarr v3-style automation patterns.

### Impact
- Root folder automation did not behave consistently.
- API configuration paths differed from the rest of the stack.
- Additional migration work was required.

### Resolution
- Moved Whisparr to a compatible branch.
- Revalidated API endpoints.
- Reconfigured automation through API.

### Lesson Learned
Before generating a deployment bundle:
- verify image source
- verify major version
- verify API compatibility
- verify configuration automation path

---

## 2. qBittorrent Version Must Be Pinned

### Event
qBittorrent was deployed using a newer version than some ARR integrations supported.

### Impact
- Direct qBittorrent API login worked.
- ARR download client validation failed.
- Whisparr reported authentication failures despite valid credentials.

### Root Cause
qBittorrent 5.2.x login behavior was incompatible with the Whisparr integration.

### Resolution
Pinned qBittorrent:

```
lscr.io/linuxserver/qbittorrent:5.1.4
```

### Lesson Learned
Avoid `latest` for infrastructure dependencies where API compatibility matters.

Pin versions for:
- qBittorrent
- ARR applications
- databases
- reverse proxies

---

## 3. Configuration-First Deployment Is Preferred

### Event
Initial setup required WebUI interaction.

### Impact
- Slower deployment.
- Increased risk of missed settings.
- Poor fit for remote/mobile workflows.

### Resolution
Moved configuration to API-driven setup:
- download clients
- root folders
- Prowlarr applications
- sync settings

### Lesson Learned
Preferred deployment sequence:

1. Generate compose
2. Deploy containers
3. Bootstrap configuration through APIs
4. Validate state
5. Commit

---

## 4. Validate Current State Before Repeating Commands

### Event
Commands were repeated after state had already changed or without confirming current state.

### Impact
- Increased troubleshooting time.
- Created unnecessary loops.

### Lesson Learned
Every command should have:
- known target
- expected output
- decision path based on output

---

## 5. Generated Artifacts Preferred Over Inline Editing

### Event
Inline shell edits were used for YAML/Dockerfile changes.

### Impact
- Increased risk of syntax corruption.
- Reduced reproducibility.

### Resolution
Use generated artifacts.

### Lesson Learned
For:
- compose files
- Dockerfiles
- scripts
- configuration files

Preferred workflow:

1. Generate file
2. Transfer file
3. Validate
4. Commit

---

## Final Deployment State

Validated components:

- qBittorrent
- Sonarr
- Radarr
- Lidarr
- Whisparr
- Prowlarr
- BookBot

Validated architecture:

- Temporary downloads remain on WyseARR
- Media library remains on DAS/Pi-SSD
- ARR services access `/media`
- qBittorrent routes through categories
- Configuration committed to Git

---

## Future Upgrade Checklist

Before changing versions:

- Check release compatibility
- Check API compatibility
- Pin versions before upgrade
- Test in disposable environment if possible
- Commit known-good state
