"""
Test script for mission steering controls (Wave E).

Tests the end-to-end flow:
1. Launch a simple mission via POST /api/runs
2. Interrupt it via POST /api/runs/<id>/interrupt
3. Resume with edited brief via POST /api/runs/<id>/resume

Usage:
    python test_steering.py

Requires the Sanic app to be running on localhost:8000.
"""
import time
import requests
import json

BASE_URL = "http://localhost:8000"

def test_launch():
    """Test mission launch endpoint"""
    print("\n=== TEST 1: Launch Mission ===")

    payload = {
        "slug": "test-steering-mission",
        "agents": [
            {
                "name": "explorer",
                "brief": "List files in the current directory and report findings",
                "tools": ["Read", "Grep", "Glob"]
            },
            {
                "name": "analyzer",
                "brief": "Analyze the file listing and identify key files",
                "tools": ["Read", "Grep"]
            }
        ],
        "synthesis": "Summarize what files exist and their purpose",
        "max_turns": 10,
        "max_concurrency": 2
    }

    response = requests.post(f"{BASE_URL}/api/runs", json=payload)

    if response.status_code == 200:
        data = response.json()
        print(f"✓ Mission launched: {data['run_id']}")
        print(f"  Slug: {data['slug']}")
        print(f"  Log: {data['log_path']}")
        return data['run_id']
    else:
        print(f"✗ Launch failed: {response.status_code}")
        print(f"  Error: {response.text}")
        return None

def test_interrupt(run_id):
    """Test mission interrupt endpoint"""
    print(f"\n=== TEST 2: Interrupt Mission {run_id} ===")

    # Wait a bit for mission to start
    time.sleep(2)

    response = requests.post(f"{BASE_URL}/api/runs/{run_id}/interrupt")

    if response.status_code == 200:
        data = response.json()
        print(f"✓ Interrupt signal sent")
        print(f"  Status: {data['status']}")
        print(f"  Sentinel: {data.get('sentinel_file')}")
        return True
    else:
        print(f"✗ Interrupt failed: {response.status_code}")
        print(f"  Error: {response.text}")
        return False

def test_resume(run_id):
    """Test mission resume endpoint"""
    print(f"\n=== TEST 3: Resume Mission {run_id} ===")

    # Wait for mission to finish or be interrupted
    time.sleep(3)

    payload = {
        "original_agents": [
            {
                "name": "explorer",
                "brief": "List files in the current directory and report findings",
                "tools": ["Read", "Grep", "Glob"]
            },
            {
                "name": "analyzer",
                "brief": "Analyze the file listing and identify key files",
                "tools": ["Read", "Grep"]
            }
        ],
        "agent_edits": [
            {
                "name": "explorer",
                "brief": "List only Python files in the current directory",
                "tools": ["Read", "Grep", "Glob"]
            }
        ],
        "synthesis": "Summarize Python files found",
        "max_turns": 10,
        "max_concurrency": 2
    }

    response = requests.post(f"{BASE_URL}/api/runs/{run_id}/resume", json=payload)

    if response.status_code == 200:
        data = response.json()
        print(f"✓ Mission resumed")
        print(f"  New run ID: {data['run_id']}")
        print(f"  Slug: {data['slug']}")
        print(f"  Log: {data['log_path']}")
        return data['run_id']
    else:
        print(f"✗ Resume failed: {response.status_code}")
        print(f"  Error: {response.text}")
        return None

def main():
    print("=" * 60)
    print("Mission Steering Controls Test Suite")
    print("=" * 60)

    # Test 1: Launch
    run_id = test_launch()
    if not run_id:
        print("\n✗ Test suite failed at launch step")
        return

    # Test 2: Interrupt
    interrupt_ok = test_interrupt(run_id)
    if not interrupt_ok:
        print("\n⚠ Interrupt test failed, but continuing...")

    # Test 3: Resume
    resumed_run_id = test_resume(run_id)
    if not resumed_run_id:
        print("\n✗ Test suite failed at resume step")
        return

    print("\n" + "=" * 60)
    print("✓ All tests completed!")
    print("=" * 60)
    print(f"\nOriginal run: {run_id}")
    print(f"Resumed run:  {resumed_run_id}")
    print(f"\nView in UI:")
    print(f"  {BASE_URL}/runs/{run_id}")
    print(f"  {BASE_URL}/runs/{resumed_run_id}")

if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.ConnectionError:
        print("\n✗ Error: Could not connect to server at localhost:8000")
        print("Please start the Sanic app first: python app.py")
    except KeyboardInterrupt:
        print("\n\n⚠ Test interrupted by user")
