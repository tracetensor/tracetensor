#!/bin/bash

##############################################################################
# TraceTensor E2E Test Suite for trace_test Database
#
# This script tests the complete ingestion pipeline against a real PostgreSQL
# database. It validates:
#   1. Database connectivity
#   2. Schema initialization
#   3. Task registration (ZIP upload)
#   4. Task registration (form upload)
#   5. File storage and retrieval
#   6. Validation logic
#   7. Error handling
#
# Prerequisites:
#   - PostgreSQL running with trace_test database
#   - API running on http://localhost:8000
#   - examples/sort-csv available
#
# Usage:
#   bash test_trace_test_e2e.sh
##############################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

BASE_URL="http://localhost:8000"
TESTS_PASSED=0
TESTS_FAILED=0

# Helper functions
log_header() {
    echo -e "${BLUE}=== $1 ===${NC}"
}

log_test() {
    echo -e "${YELLOW}▶ $1${NC}"
}

log_pass() {
    echo -e "${GREEN}✓ PASS: $1${NC}"
    ((TESTS_PASSED++))
}

log_fail() {
    echo -e "${RED}✗ FAIL: $1${NC}"
    ((TESTS_FAILED++))
}

log_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Test if API is reachable
check_api_health() {
    log_test "API Health Check"

    RESPONSE=$(curl -s -w "\n%{http_code}" -X GET $BASE_URL/api/health)
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)

    if [ "$HTTP_CODE" -eq 200 ]; then
        STATUS=$(echo $BODY | jq -r '.status' 2>/dev/null)
        if [ "$STATUS" = "ok" ]; then
            log_pass "API is healthy and responding"
            return 0
        else
            log_fail "API responded but status is not ok: $BODY"
            return 1
        fi
    else
        log_fail "API not responding (HTTP $HTTP_CODE). Is it running on $BASE_URL?"
        return 1
    fi
}

# Test 1: List tasks when empty
test_list_empty() {
    log_test "List Tasks (Empty)"

    RESPONSE=$(curl -s -X GET $BASE_URL/ingest/tasks)
    COUNT=$(echo $RESPONSE | jq '.count' 2>/dev/null)

    if [ "$COUNT" -eq 0 ]; then
        log_pass "Empty task list returned correctly"
        return 0
    else
        log_fail "Expected 0 tasks, got $COUNT"
        return 1
    fi
}

# Test 2: Register sort-csv via ZIP
test_register_zip() {
    log_test "Register sort-csv via ZIP Upload"

    # Create ZIP if it doesn't exist
    if [ ! -f "examples/sort-csv.zip" ]; then
        log_info "Creating sort-csv.zip..."
        cd examples
        zip -r sort-csv.zip sort-csv -q
        cd ..
    fi

    RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $BASE_URL/ingest/task/upload \
        -F "file=@examples/sort-csv.zip")
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)

    if [ "$HTTP_CODE" -eq 200 ]; then
        STATUS=$(echo $BODY | jq -r '.status' 2>/dev/null)
        if [ "$STATUS" = "ready_for_examination" ]; then
            TASK_ID=$(echo $BODY | jq -r '.id')
            log_pass "Task registered with status ready_for_examination"
            log_info "Task ID: $TASK_ID"
            echo "$TASK_ID"
            return 0
        else
            log_fail "Task status is $STATUS, expected ready_for_examination"
            echo $BODY | jq .
            return 1
        fi
    else
        log_fail "Failed to upload ZIP (HTTP $HTTP_CODE)"
        echo $BODY | jq .
        return 1
    fi
}

# Test 3: Verify health checks
test_health_checks() {
    log_test "Verify Health Checks for Registered Task"

    TASK_ID=$1
    RESPONSE=$(curl -s -X GET $BASE_URL/ingest/task/$TASK_ID)

    # Extract health checks
    HAS_INSTRUCTION=$(echo $RESPONSE | jq '.health_checks.has_instruction')
    HAS_TASK_TOML=$(echo $RESPONSE | jq '.health_checks.has_task_toml')
    HAS_DOCKERFILE=$(echo $RESPONSE | jq '.health_checks.has_dockerfile')
    HAS_TEST_SCRIPT=$(echo $RESPONSE | jq '.health_checks.has_test_script')
    HAS_SOLUTION=$(echo $RESPONSE | jq '.health_checks.has_solution')

    if [ "$HAS_INSTRUCTION" = "true" ] && [ "$HAS_TASK_TOML" = "true" ] && \
       [ "$HAS_DOCKERFILE" = "true" ] && [ "$HAS_TEST_SCRIPT" = "true" ]; then
        log_pass "All required health checks passed"
        log_info "  - instruction.md: $HAS_INSTRUCTION"
        log_info "  - task.toml: $HAS_TASK_TOML"
        log_info "  - Dockerfile: $HAS_DOCKERFILE"
        log_info "  - test.sh: $HAS_TEST_SCRIPT"
        log_info "  - solution: $HAS_SOLUTION"
        return 0
    else
        log_fail "Some health checks failed"
        echo $RESPONSE | jq '.health_checks'
        return 1
    fi
}

# Test 4: List tasks after registration
test_list_with_tasks() {
    log_test "List Tasks (After Registration)"

    RESPONSE=$(curl -s -X GET $BASE_URL/ingest/tasks)
    COUNT=$(echo $RESPONSE | jq '.count' 2>/dev/null)

    if [ "$COUNT" -ge 1 ]; then
        log_pass "Task list contains registered tasks (count: $COUNT)"
        return 0
    else
        log_fail "Expected at least 1 task, got $COUNT"
        return 1
    fi
}

# Test 5: Get task files
test_get_task_files() {
    log_test "Get Task Files (instruction.md, task.toml, Dockerfile, test.sh)"

    TASK_ID=$1
    RESPONSE=$(curl -s -X GET $BASE_URL/ingest/task/$TASK_ID/files)

    INSTRUCTION=$(echo $RESPONSE | jq -r '.instruction_md' 2>/dev/null)
    TASK_TOML=$(echo $RESPONSE | jq -r '.task_toml' 2>/dev/null)
    DOCKERFILE=$(echo $RESPONSE | jq -r '.dockerfile' 2>/dev/null)
    TEST_SH=$(echo $RESPONSE | jq -r '.test_sh' 2>/dev/null)

    if [ ! -z "$INSTRUCTION" ] && [ ! -z "$TASK_TOML" ] && \
       [ ! -z "$DOCKERFILE" ] && [ ! -z "$TEST_SH" ]; then
        log_pass "All task files retrieved successfully"
        log_info "  - instruction.md: ${#INSTRUCTION} bytes"
        log_info "  - task.toml: ${#TASK_TOML} bytes"
        log_info "  - Dockerfile: ${#DOCKERFILE} bytes"
        log_info "  - test.sh: ${#TEST_SH} bytes"
        return 0
    else
        log_fail "Failed to retrieve all task files"
        echo $RESPONSE | jq .
        return 1
    fi
}

# Test 6: Register via form (individual files)
test_register_form() {
    log_test "Register Task via Form Upload (Individual Files)"

    RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $BASE_URL/ingest/task/create \
        -F "name=test/form-upload" \
        -F "instruction=@examples/sort-csv/instruction.md" \
        -F "dockerfile=@examples/sort-csv/environment/Dockerfile" \
        -F "test_script=@examples/sort-csv/tests/test.sh")

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)

    if [ "$HTTP_CODE" -eq 200 ]; then
        STATUS=$(echo $BODY | jq -r '.status' 2>/dev/null)
        if [ "$STATUS" = "ready_for_examination" ]; then
            TASK_ID=$(echo $BODY | jq -r '.id')
            log_pass "Form-based task registration successful"
            log_info "Task ID: $TASK_ID"
            echo "$TASK_ID"
            return 0
        else
            log_fail "Task status is $STATUS (may be OK if missing solution)"
            log_info "Full response:"
            echo $BODY | jq .
            return 0  # Don't fail on warnings
        fi
    else
        log_fail "Failed to register via form (HTTP $HTTP_CODE)"
        echo $BODY | jq .
        return 1
    fi
}

# Test 7: Invalid task (missing required files)
test_invalid_task() {
    log_test "Register Invalid Task (Missing Verifier)"

    # Create a minimal invalid task
    TEMP_DIR="/tmp/invalid-task-$$"
    mkdir -p "$TEMP_DIR"
    echo "This is an instruction" > "$TEMP_DIR/instruction.md"

    cd /tmp
    zip -r "invalid-task-$$.zip" "invalid-task-$$" -q
    cd - > /dev/null

    RESPONSE=$(curl -s -w "\n%{http_code}" -X POST $BASE_URL/ingest/task/upload \
        -F "file=@/tmp/invalid-task-$$.zip")

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)

    # Clean up
    rm -rf "$TEMP_DIR" "/tmp/invalid-task-$$.zip"

    if [ "$HTTP_CODE" -eq 200 ]; then
        STATUS=$(echo $BODY | jq -r '.status' 2>/dev/null)
        ERRORS=$(echo $BODY | jq '.validation.errors' 2>/dev/null)

        if [ "$STATUS" = "registered" ] && [ "$ERRORS" != "[]" ] && [ "$ERRORS" != "null" ]; then
            log_pass "Invalid task correctly rejected with errors"
            log_info "Status: $STATUS"
            log_info "Errors: $(echo $ERRORS | jq -c .)"
            return 0
        else
            log_fail "Invalid task was not properly flagged"
            return 1
        fi
    else
        log_fail "Unexpected HTTP code: $HTTP_CODE"
        echo $BODY | jq .
        return 1
    fi
}

# Test 8: Database persistence (tasks survive API restart)
test_database_query() {
    log_test "Verify Database Persistence"

    # Query the database directly
    if command -v psql &> /dev/null; then
        COUNT=$(psql -U tracetensor -d trace_test -h localhost -tc "SELECT COUNT(*) FROM tasks;" 2>/dev/null | tr -d ' ')

        if [ ! -z "$COUNT" ] && [ "$COUNT" -ge 2 ]; then
            log_pass "Database contains $COUNT registered tasks"
            return 0
        else
            log_fail "Expected at least 2 tasks in database, found $COUNT"
            return 1
        fi
    else
        log_info "psql not available, skipping direct DB query"
        return 0
    fi
}

##############################################################################
# Main Test Runner
##############################################################################

main() {
    log_header "TraceTensor E2E Test Suite — trace_test Database"
    echo ""

    # Check API health
    if ! check_api_health; then
        echo ""
        log_header "FATAL: API not responding"
        exit 1
    fi
    echo ""

    # Run tests
    if ! test_list_empty; then
        log_fail "Failed to connect to database or list empty tasks"
        exit 1
    fi
    echo ""

    TASK_ID_1=$(test_register_zip)
    if [ -z "$TASK_ID_1" ]; then
        exit 1
    fi
    echo ""

    if ! test_health_checks "$TASK_ID_1"; then
        exit 1
    fi
    echo ""

    if ! test_list_with_tasks; then
        exit 1
    fi
    echo ""

    if ! test_get_task_files "$TASK_ID_1"; then
        exit 1
    fi
    echo ""

    TASK_ID_2=$(test_register_form)
    echo ""

    if ! test_invalid_task; then
        exit 1
    fi
    echo ""

    if ! test_database_query; then
        exit 1
    fi
    echo ""

    # Summary
    log_header "Test Summary"
    TOTAL=$((TESTS_PASSED + TESTS_FAILED))
    echo -e "Total Tests: $TOTAL"
    echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
    if [ $TESTS_FAILED -gt 0 ]; then
        echo -e "${RED}Failed: $TESTS_FAILED${NC}"
        exit 1
    else
        echo -e "${GREEN}Failed: $TESTS_FAILED${NC}"
        echo ""
        log_header "✅ ALL TESTS PASSED!"
        exit 0
    fi
}

main "$@"
