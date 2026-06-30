"""Tests for SLURM output parsing helpers in jobs blueprint."""

from __future__ import annotations

from blueprints.jobs import (
    _parse_sacct_output,
    _parse_sinfo_output,
    _parse_time_to_seconds,
)

SAMPLE_SINFO_OUTPUT = """
PARTITION          AVAIL  TIMELIMIT   NODES(A/I/O/T)  NODELIST
interactive           up    2:00:00       71/24/4/99  c[0136-0149,0151-0235]
express               up   12:00:00        8/12/0/20  d[001-020]
"""

SAMPLE_SACCT_OUTPUT = (
    "12345|my job|COMPLETED|express|2024-01-01T10:00:00|"
    "2024-01-01T11:00:00|01:00:00|00:30:00|4|1024M|4|02:00:00"
)


def test_parse_sinfo_output_extracts_partition_fields() -> None:
    partitions = _parse_sinfo_output(SAMPLE_SINFO_OUTPUT)

    assert len(partitions) == 2
    interactive = next(p for p in partitions if p["name"] == "interactive")
    assert interactive["allocated"] == 71
    assert interactive["idle"] == 24
    assert interactive["total"] == 99
    assert interactive["availability_pct"] == round(24 / 99 * 100, 1)


def test_parse_sacct_output_builds_job_record() -> None:
    jobs = _parse_sacct_output(SAMPLE_SACCT_OUTPUT)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == "12345"
    assert job["name"] == "my job"
    assert job["state"] == "COMPLETED"
    assert job["partition"] == "express"
    assert job["memory_mb"] == 1024.0


def test_parse_time_to_seconds_handles_hms_and_day_formats() -> None:
    assert _parse_time_to_seconds("01:30:00") == 5400
    assert _parse_time_to_seconds("1-02:00:00") == 93600
    assert _parse_time_to_seconds("N/A") == 0
