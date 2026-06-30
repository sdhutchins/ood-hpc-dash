# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0]

### Added

- Lua-based spider cache parser that reads Lmod's `spiderT.lua` directly
- Configurable spider cache path via `OOD_HPC_DASH_SPIDER_CACHE` env var
- Shared `_run_slurm_command` helper for SLURM binary execution
- `git-status-checker` integration as primary project scanner with manual fallback
- App factory pattern (`create_app`) with proper Flask structure
- CSRF protection on state-changing endpoints
- Secure `SECRET_KEY` generation and management
- Path validation for code editor and project directories
- `pytest` test suite with parser, route, and utility coverage
- Dockerfile for local development and testing
- Development dependencies in `requirements-dev.txt`

### Changed

- Module loading reads Lmod system cache file instead of subprocess calls
- Consolidated three `os.walk` passes in project drift checking into one
- SLURM commands (`sinfo`, `squeue`, `sacct`) use shared execution helper
- Moved operational scripts to `scripts/archive/`

### Removed

- `module --redirect spider` subprocess-based module loading
- Unused `_format_time_limit` function from jobs blueprint
- Unused module description update endpoints
- Slurm load cache dead code path
- Redundant path validation functions in utils

## [0.1.0] - Initial Development

### Added

- Software modules browser with category filtering, search, and version display
- Cluster partition monitoring with real-time job and resource status
- Conda environment viewer organized by location
- Web-based code editor integration
- SSE-based module refresh streaming
- Module categorization from JSON configuration
- Project git status scanning and drift detection
- Environment export and dependency history tracking
