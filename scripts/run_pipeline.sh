#!/bin/bash
#
# TDAD Pipeline Runner
# ====================
# Runs the full TDAD pipeline for a given spec and collects results.
#
# Usage:
#   ./scripts/run_pipeline.sh                               # Full pipeline: supportops/v1 + supportops/v2
#   ./scripts/run_pipeline.sh supportops                    # Both v1 and v2 for supportops
#   ./scripts/run_pipeline.sh supportops/v1                 # Just v1
#   ./scripts/run_pipeline.sh supportops/v2                 # Just v2
#   ./scripts/run_pipeline.sh supportops/v1 --skip-init     # Skip volume init
#   ./scripts/run_pipeline.sh supportops/v2 --skip-testsmith
#
# Output:
#   - Pipeline logs with timing
#   - Results summary in JSON format
#   - Appends to pipeline_results.json for historical tracking
#

set -euo pipefail

# Check dependencies
for cmd in docker jq bc; do
    if ! command -v $cmd &> /dev/null; then
        echo "Error: $cmd is required but not installed"
        exit 1
    fi
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Default values
SKIP_INIT=false
SKIP_TESTSMITH=false
SKIP_COMPILER=false
SKIP_MUTATION=false
SPEC_INPUT=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-init)
            SKIP_INIT=true
            shift
            ;;
        --skip-testsmith)
            SKIP_TESTSMITH=true
            shift
            ;;
        --skip-compiler)
            SKIP_COMPILER=true
            shift
            ;;
        --skip-mutation)
            SKIP_MUTATION=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [spec-name] [options]"
            echo ""
            echo "Arguments:"
            echo "  spec-name    Spec to run (optional, defaults to full pipeline)"
            echo "               Examples: supportops (both v1+v2), supportops/v1, supportops/v2"
            echo ""
            echo "Options:"
            echo "  --skip-init       Skip init-volumes stage"
            echo "  --skip-testsmith  Skip test generation (use existing tests)"
            echo "  --skip-compiler   Skip compilation (use existing prompt)"
            echo "  --skip-mutation   Skip mutation testing (MS calculation)"
            echo "  --help, -h        Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                          # Full pipeline: supportops v1 + v2"
            echo "  $0 supportops               # Both versions"
            echo "  $0 supportops/v1            # Just v1"
            echo "  $0 helloworld/v1            # HelloWorld v1 (minimal spec)"
            exit 0
            ;;
        *)
            if [[ -z "$SPEC_INPUT" ]]; then
                SPEC_INPUT="$1"
            else
                echo "Error: Unknown argument: $1"
                exit 1
            fi
            shift
            ;;
    esac
done

# Parse spec input - default to supportops if not provided
if [[ -z "$SPEC_INPUT" ]]; then
    SPEC_INPUT="supportops"
fi

# Parse spec name and version
IFS='/' read -r SPEC_NAME SPEC_VERSION <<< "$SPEC_INPUT"

# Determine which versions to run
if [[ -z "$SPEC_VERSION" ]]; then
    # No version specified - run both v1 and v2
    VERSIONS_TO_RUN="v1 v2"
else
    VERSIONS_TO_RUN="$SPEC_VERSION"
fi

# Helper function to set service names for a version
set_services_for_version() {
    local version="$1"

    # Determine service suffix based on spec name
    # supportops uses no suffix (default), others use -specname suffix
    local spec_suffix=""
    if [[ "$SPEC_NAME" != "supportops" ]]; then
        spec_suffix="-${SPEC_NAME}"
    fi

    if [[ "$version" == "v1" ]]; then
        TESTSMITH_SERVICE="testsmith${spec_suffix}"
        COMPILER_SERVICE="compiler${spec_suffix}"
        EVALUATE_SERVICE="evaluate${spec_suffix}"
        SURS_SERVICE=""
        IS_V2=false
    else
        TESTSMITH_SERVICE="testsmith${spec_suffix}-v2"
        COMPILER_SERVICE="compiler${spec_suffix}-v2"
        EVALUATE_SERVICE="evaluate${spec_suffix}-v2"
        SURS_SERVICE="evaluate${spec_suffix}-surs"
        IS_V2=true
    fi
}

# Results tracking
RESULTS_DIR="$REPO_ROOT/results"
RESULTS_FILE="$RESULTS_DIR/all_runs.json"
mkdir -p "$RESULTS_DIR"
RUN_ID=$(date +%Y%m%d_%H%M%S)
RUN_DATE=$(date +%Y-%m-%d)
RUN_TIME=$(date +%H:%M:%S)

# Metrics (using regular variables for bash 3.2 compatibility)
METRIC_seed_vpr_passed=0
METRIC_seed_vpr_total=0
METRIC_seed_hpr_passed=0
METRIC_seed_hpr_total=0
METRIC_vpr_passed=0
METRIC_vpr_total=0
METRIC_hpr_passed=0
METRIC_hpr_total=0
METRIC_surs_passed=0
METRIC_surs_total=0
METRIC_compiler_iterations=0
METRIC_testsmith_visible_files=0
METRIC_testsmith_hidden_files=0
METRIC_testsmith_time=0
METRIC_compiler_time=0
METRIC_evaluate_time=0
METRIC_mutation_time=0
METRIC_surs_time=0
METRIC_total_time=0
# Mutation testing metrics
METRIC_mutation_total=0
METRIC_mutation_activated=0
METRIC_mutation_killed=0
METRIC_mutation_survived=0
METRIC_mutation_score=0
# Cost tracking
METRIC_testsmith_cost_usd=0
METRIC_testsmith_input_tokens=0
METRIC_testsmith_cache_creation_tokens=0
METRIC_testsmith_cache_read_tokens=0
METRIC_testsmith_output_tokens=0
METRIC_compiler_cost_usd=0
METRIC_compiler_input_tokens=0
METRIC_compiler_cache_creation_tokens=0
METRIC_compiler_cache_read_tokens=0
METRIC_compiler_output_tokens=0
METRIC_total_cost_usd=0
METRIC_total_input_tokens=0
METRIC_total_cache_creation_tokens=0
METRIC_total_cache_read_tokens=0
METRIC_total_output_tokens=0
# Test execution costs (from pytest runs during evaluate)
METRIC_test_cost_usd=0
METRIC_test_input_tokens=0
METRIC_test_cache_creation_tokens=0
METRIC_test_cache_read_tokens=0
METRIC_test_output_tokens=0
METRIC_test_num_tests=0

# Stage results
STAGE_init_volumes="skipped"
STAGE_testsmith="skipped"
STAGE_compiler="skipped"
STAGE_evaluate="skipped"
STAGE_mutation="skipped"
STAGE_evaluate_surs="skipped"

# Log functions
log_header() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

log_stage() {
    echo -e "${CYAN}▶ $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

log_info() {
    echo -e "${YELLOW}ℹ️  $1${NC}"
}

log_warn() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

# Timer functions
start_timer() {
    TIMER_START=$(date +%s)
}

get_elapsed() {
    local end=$(date +%s)
    echo $((end - TIMER_START))
}

# Format bc output to ensure leading zero (bc outputs .5 instead of 0.5)
format_decimal() {
    local value="$1"
    local scale="${2:-6}"  # default to 6 decimal places
    # Use awk to ensure proper formatting with leading zero
    echo "$value" | awk "{printf \"%.${scale}f\", \$0}"
}

# Parse pytest output for pass/fail counts
# Expected format: "62 passed, 3 failed in 45.2s" or "62 passed in 45.2s"
parse_pytest_summary() {
    local output="$1"
    local passed=0
    local failed=0

    # Extract the summary line (last line with "passed" in it)
    local summary=$(echo "$output" | grep -E '[0-9]+ passed' | tail -1)

    if [[ -n "$summary" ]]; then
        # Extract passed count
        passed=$(echo "$summary" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo "0")
        # Extract failed count (may not exist)
        failed=$(echo "$summary" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' || echo "0")
    fi

    echo "$passed $failed"
}

# Parse compiler iterations from output
parse_compiler_iterations() {
    local output="$1"
    # Look for "Tests passed on iteration N" or "ITERATION N/M"
    local iteration=$(echo "$output" | grep -oE 'Tests passed on iteration [0-9]+' | grep -oE '[0-9]+' | tail -1)
    if [[ -z "$iteration" ]]; then
        # Fallback: count iteration markers
        iteration=$(echo "$output" | grep -cE '🔄 ITERATION [0-9]+' || echo "0")
    fi
    echo "$iteration"
}

# Parse testsmith output for file counts
parse_testsmith_summary() {
    local output="$1"
    local visible=0
    local hidden=0

    # Look for file creation messages
    visible=$(echo "$output" | grep -cE 'Created.*test_.*\.py' | head -1 || echo "0")
    # Or look for summary at the end
    if [[ "$output" =~ ([0-9]+)\ visible.*([0-9]+)\ hidden ]]; then
        visible="${BASH_REMATCH[1]}"
        hidden="${BASH_REMATCH[2]}"
    fi

    echo "$visible $hidden"
}

# Parse COST_SUMMARY line from output
# Format: COST_SUMMARY: input_tokens=N cache_creation=N cache_read=N output_tokens=N total_cost_usd=X.XXXXXX
# Returns: input_tokens cache_creation cache_read output_tokens cost_usd
parse_cost_summary() {
    local output="$1"
    local cost_line=$(echo "$output" | grep -E '^COST_SUMMARY:' | tail -1)

    if [[ -n "$cost_line" ]]; then
        local input_tokens=$(echo "$cost_line" | grep -oE 'input_tokens=[0-9]+' | cut -d= -f2 || echo "0")
        local cache_creation=$(echo "$cost_line" | grep -oE 'cache_creation=[0-9]+' | cut -d= -f2 || echo "0")
        local cache_read=$(echo "$cost_line" | grep -oE 'cache_read=[0-9]+' | cut -d= -f2 || echo "0")
        local output_tokens=$(echo "$cost_line" | grep -oE 'output_tokens=[0-9]+' | cut -d= -f2 || echo "0")
        local cost_usd=$(echo "$cost_line" | grep -oE 'total_cost_usd=[0-9.]+' | cut -d= -f2 || echo "0")
        echo "${input_tokens:-0} ${cache_creation:-0} ${cache_read:-0} ${output_tokens:-0} ${cost_usd:-0}"
    else
        echo "0 0 0 0 0"
    fi
}

# Parse TEST_COST_SUMMARY line from pytest output
# Format: TEST_COST_SUMMARY: tests=N input_tokens=N cache_creation=N cache_read=N output_tokens=N total_cost_usd=X.XXXXXX
# Returns: tests input_tokens cache_creation cache_read output_tokens cost_usd
parse_test_cost_summary() {
    local output="$1"
    local cost_line=$(echo "$output" | grep -E '^TEST_COST_SUMMARY:' | tail -1)

    if [[ -n "$cost_line" ]]; then
        local num_tests=$(echo "$cost_line" | grep -oE 'tests=[0-9]+' | cut -d= -f2 || echo "0")
        local input_tokens=$(echo "$cost_line" | grep -oE 'input_tokens=[0-9]+' | cut -d= -f2 || echo "0")
        local cache_creation=$(echo "$cost_line" | grep -oE 'cache_creation=[0-9]+' | cut -d= -f2 || echo "0")
        local cache_read=$(echo "$cost_line" | grep -oE 'cache_read=[0-9]+' | cut -d= -f2 || echo "0")
        local output_tokens=$(echo "$cost_line" | grep -oE 'output_tokens=[0-9]+' | cut -d= -f2 || echo "0")
        local cost_usd=$(echo "$cost_line" | grep -oE 'total_cost_usd=[0-9.]+' | cut -d= -f2 || echo "0")
        echo "${num_tests:-0} ${input_tokens:-0} ${cache_creation:-0} ${cache_read:-0} ${output_tokens:-0} ${cost_usd:-0}"
    else
        echo "0 0 0 0 0 0"
    fi
}

# Run pipeline for a single version
run_version_pipeline() {
    local version="$1"
    local VERSION_START=$(date +%s)

    # Set services for this version
    set_services_for_version "$version"

    # Reset metrics for this version run
    METRIC_seed_vpr_passed=0
    METRIC_seed_vpr_total=0
    METRIC_seed_hpr_passed=0
    METRIC_seed_hpr_total=0
    METRIC_vpr_passed=0
    METRIC_vpr_total=0
    METRIC_hpr_passed=0
    METRIC_hpr_total=0
    METRIC_surs_passed=0
    METRIC_surs_total=0
    METRIC_compiler_iterations=0
    METRIC_testsmith_visible_files=0
    METRIC_testsmith_hidden_files=0
    METRIC_testsmith_time=0
    METRIC_compiler_time=0
    METRIC_evaluate_time=0
    METRIC_surs_time=0
    METRIC_testsmith_cost_usd=0
    METRIC_testsmith_input_tokens=0
    METRIC_testsmith_cache_creation_tokens=0
    METRIC_testsmith_cache_read_tokens=0
    METRIC_testsmith_output_tokens=0
    METRIC_compiler_cost_usd=0
    METRIC_compiler_input_tokens=0
    METRIC_compiler_cache_creation_tokens=0
    METRIC_compiler_cache_read_tokens=0
    METRIC_compiler_output_tokens=0
    METRIC_test_cost_usd=0
    METRIC_test_input_tokens=0
    METRIC_test_cache_creation_tokens=0
    METRIC_test_cache_read_tokens=0
    METRIC_test_output_tokens=0
    METRIC_test_num_tests=0
    STAGE_testsmith="skipped"
    STAGE_compiler="skipped"
    STAGE_evaluate="skipped"
    STAGE_evaluate_surs="skipped"

    log_header "Pipeline: $SPEC_NAME/$version"

    # ═══════════════════════════════════════════════════════════════════
    # TestSmith (Test Generation)
    # ═══════════════════════════════════════════════════════════════════
    if [[ "$SKIP_TESTSMITH" == "false" ]]; then
        log_stage "TestSmith: Running $TESTSMITH_SERVICE..."
        start_timer

        # Stream output in real-time while capturing it
        local testsmith_tmpfile=$(mktemp)
        set +e
        docker compose run --rm "$TESTSMITH_SERVICE" 2>&1 | tee "$testsmith_tmpfile"
        local testsmith_exit=${PIPESTATUS[0]}
        set -e
        local testsmith_output=$(cat "$testsmith_tmpfile")
        rm -f "$testsmith_tmpfile"

        METRIC_testsmith_time=$(get_elapsed)

        if [[ $testsmith_exit -eq 0 ]]; then
            STAGE_testsmith="pass"
            log_success "testsmith completed in ${METRIC_testsmith_time}s"

            # Parse cost from output (5 values: input, cache_creation, cache_read, output, cost)
            read -r ts_in_tok ts_cache_create ts_cache_read ts_out_tok ts_cost <<< $(parse_cost_summary "$testsmith_output")
            METRIC_testsmith_input_tokens=$ts_in_tok
            METRIC_testsmith_cache_creation_tokens=$ts_cache_create
            METRIC_testsmith_cache_read_tokens=$ts_cache_read
            METRIC_testsmith_output_tokens=$ts_out_tok
            METRIC_testsmith_cost_usd=$ts_cost
            if [[ "$ts_cost" != "0" ]]; then
                local ts_total_in=$((ts_in_tok + ts_cache_create + ts_cache_read))
                log_info "TestSmith cost: \$${ts_cost} USD (${ts_total_in} in / ${ts_out_tok} out tokens)"
            fi

            # Count generated test files (using docker to access volumes)
            local visible_count=$(docker compose run --rm -T --entrypoint "" "$EVALUATE_SERVICE" \
                find /workspace/tests_visible/core/$SPEC_NAME/$version -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ')
            local hidden_count=$(docker compose run --rm -T --entrypoint "" "$EVALUATE_SERVICE" \
                find /workspace/tests_hidden/core/$SPEC_NAME/$version -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ')
            METRIC_testsmith_visible_files=${visible_count:-0}
            METRIC_testsmith_hidden_files=${hidden_count:-0}
            log_info "Generated ${METRIC_testsmith_visible_files} visible, ${METRIC_testsmith_hidden_files} hidden test files"

            # Parse seed baseline from testsmith output (tests run against seed prompt)
            # Format: SEED_BASELINE: visible_passed=X visible_failed=Y hidden_passed=Z hidden_failed=W
            local seed_line=$(echo "$testsmith_output" | grep -E '^SEED_BASELINE:' | tail -1)
            if [[ -n "$seed_line" ]]; then
                METRIC_seed_vpr_passed=$(echo "$seed_line" | grep -oE 'visible_passed=[0-9]+' | cut -d= -f2 || echo "0")
                local seed_vpr_failed=$(echo "$seed_line" | grep -oE 'visible_failed=[0-9]+' | cut -d= -f2 || echo "0")
                METRIC_seed_vpr_total=$((METRIC_seed_vpr_passed + seed_vpr_failed))
                METRIC_seed_hpr_passed=$(echo "$seed_line" | grep -oE 'hidden_passed=[0-9]+' | cut -d= -f2 || echo "0")
                local seed_hpr_failed=$(echo "$seed_line" | grep -oE 'hidden_failed=[0-9]+' | cut -d= -f2 || echo "0")
                METRIC_seed_hpr_total=$((METRIC_seed_hpr_passed + seed_hpr_failed))

                if [[ $METRIC_seed_vpr_total -gt 0 ]]; then
                    local seed_vpr_pct=$(echo "scale=1; ${METRIC_seed_vpr_passed} * 100 / ${METRIC_seed_vpr_total}" | bc)
                    log_info "Seed VPR: ${METRIC_seed_vpr_passed}/${METRIC_seed_vpr_total} (${seed_vpr_pct}%)"
                fi
                if [[ $METRIC_seed_hpr_total -gt 0 ]]; then
                    local seed_hpr_pct=$(echo "scale=1; ${METRIC_seed_hpr_passed} * 100 / ${METRIC_seed_hpr_total}" | bc)
                    log_info "Seed HPR: ${METRIC_seed_hpr_passed}/${METRIC_seed_hpr_total} (${seed_hpr_pct}%)"
                fi
            fi
        else
            STAGE_testsmith="fail"
            log_error "testsmith failed (exit code: $testsmith_exit)"
            # Save partial results and return (skip compiler/evaluate/mutation/surs)
            save_version_results "$version"
            return 1
        fi
    else
        log_info "Skipping testsmith (--skip-testsmith)"
        # Count existing test files (using docker to access volumes)
        local visible_count=$(docker compose run --rm -T --entrypoint "" "$EVALUATE_SERVICE" \
            find /workspace/tests_visible/core/$SPEC_NAME/$version -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ')
        local hidden_count=$(docker compose run --rm -T --entrypoint "" "$EVALUATE_SERVICE" \
            find /workspace/tests_hidden/core/$SPEC_NAME/$version -name 'test_*.py' 2>/dev/null | wc -l | tr -d ' ')
        METRIC_testsmith_visible_files=${visible_count:-0}
        METRIC_testsmith_hidden_files=${hidden_count:-0}
        log_info "Using existing tests: ${METRIC_testsmith_visible_files} visible, ${METRIC_testsmith_hidden_files} hidden"
    fi

    # ═══════════════════════════════════════════════════════════════════
    # Compiler (Prompt Compilation)
    # ═══════════════════════════════════════════════════════════════════
    if [[ "$SKIP_COMPILER" == "false" ]]; then
        log_stage "Compiler: Running $COMPILER_SERVICE..."
        start_timer

        # Stream output in real-time while capturing it
        local compiler_tmpfile=$(mktemp)
        set +e
        docker compose run --rm "$COMPILER_SERVICE" 2>&1 | tee "$compiler_tmpfile"
        local compiler_exit=${PIPESTATUS[0]}
        set -e
        local compiler_output=$(cat "$compiler_tmpfile")
        rm -f "$compiler_tmpfile"

        METRIC_compiler_time=$(get_elapsed)

        if [[ $compiler_exit -eq 0 ]]; then
            STAGE_compiler="pass"

            # Parse VPR from output
            read -r vpr_passed vpr_failed <<< $(parse_pytest_summary "$compiler_output")
            METRIC_vpr_passed=$vpr_passed
            METRIC_vpr_total=$((vpr_passed + vpr_failed))

            # Parse iterations
            METRIC_compiler_iterations=$(parse_compiler_iterations "$compiler_output")

            # Parse cost from output (5 values: input, cache_creation, cache_read, output, cost)
            read -r cmp_in_tok cmp_cache_create cmp_cache_read cmp_out_tok cmp_cost <<< $(parse_cost_summary "$compiler_output")
            METRIC_compiler_input_tokens=$cmp_in_tok
            METRIC_compiler_cache_creation_tokens=$cmp_cache_create
            METRIC_compiler_cache_read_tokens=$cmp_cache_read
            METRIC_compiler_output_tokens=$cmp_out_tok
            METRIC_compiler_cost_usd=$cmp_cost

            log_success "compiler completed in ${METRIC_compiler_time}s"
            log_info "VPR: ${METRIC_vpr_passed}/${METRIC_vpr_total} ($(echo "scale=1; ${METRIC_vpr_passed} * 100 / ${METRIC_vpr_total}" | bc)%)"
            log_info "Iterations: ${METRIC_compiler_iterations}"
            if [[ "$cmp_cost" != "0" ]]; then
                local cmp_total_in=$((cmp_in_tok + cmp_cache_create + cmp_cache_read))
                log_info "Compiler cost: \$${cmp_cost} USD (${cmp_total_in} in / ${cmp_out_tok} out tokens)"
            fi
        else
            STAGE_compiler="fail"
            log_error "compiler failed (exit code: $compiler_exit)"
            # Save partial results and return (skip evaluate/mutation/surs)
            save_version_results "$version"
            return 1
        fi
    else
        log_info "Skipping compiler (--skip-compiler)"
    fi

    # ═══════════════════════════════════════════════════════════════════
    # Evaluate (HPR Measurement)
    # ═══════════════════════════════════════════════════════════════════
    log_stage "Evaluate: Running $EVALUATE_SERVICE..."
    start_timer

    # Stream output in real-time while capturing it
    local evaluate_tmpfile=$(mktemp)
    set +e
    docker compose run --rm "$EVALUATE_SERVICE" 2>&1 | tee "$evaluate_tmpfile"
    local evaluate_exit=${PIPESTATUS[0]}
    set -e
    local evaluate_output=$(cat "$evaluate_tmpfile")
    rm -f "$evaluate_tmpfile"

    METRIC_evaluate_time=$(get_elapsed)

    if [[ $evaluate_exit -eq 0 ]]; then
        STAGE_evaluate="pass"
        read -r hpr_passed hpr_failed <<< $(parse_pytest_summary "$evaluate_output")
        METRIC_hpr_passed=$hpr_passed
        METRIC_hpr_total=$((hpr_passed + hpr_failed))
        log_success "evaluate completed in ${METRIC_evaluate_time}s"
    else
        # Parse even on failure to get metrics
        read -r hpr_passed hpr_failed <<< $(parse_pytest_summary "$evaluate_output")
        METRIC_hpr_passed=$hpr_passed
        METRIC_hpr_total=$((hpr_passed + hpr_failed))

        if [[ ${METRIC_hpr_total} -gt 0 ]]; then
            STAGE_evaluate="pass"  # Tests ran, some failed - that's expected
            log_success "evaluate completed in ${METRIC_evaluate_time}s"
        else
            STAGE_evaluate="fail"
            log_error "evaluate failed (exit code: $evaluate_exit)"
        fi
    fi

    local hpr_percent=0
    if [[ ${METRIC_hpr_total} -gt 0 ]]; then
        hpr_percent=$(echo "scale=1; ${METRIC_hpr_passed} * 100 / ${METRIC_hpr_total}" | bc)
    fi
    log_info "HPR: ${METRIC_hpr_passed}/${METRIC_hpr_total} ($hpr_percent%)"

    # Parse test execution costs from evaluate output
    read -r test_num test_in_tok test_cache_create test_cache_read test_out_tok test_cost <<< $(parse_test_cost_summary "$evaluate_output")
    METRIC_test_num_tests=$test_num
    METRIC_test_input_tokens=$test_in_tok
    METRIC_test_cache_creation_tokens=$test_cache_create
    METRIC_test_cache_read_tokens=$test_cache_read
    METRIC_test_output_tokens=$test_out_tok
    METRIC_test_cost_usd=$test_cost
    if [[ "$test_cost" != "0" ]]; then
        local test_total_in=$((test_in_tok + test_cache_create + test_cache_read))
        log_info "Test API cost: \$${test_cost} USD (${test_total_in} in / ${test_out_tok} out tokens from ${test_num} tests)"
    fi

    # ═══════════════════════════════════════════════════════════════════
    # Mutation Testing (MS Measurement)
    # ═══════════════════════════════════════════════════════════════════
    if [[ "$SKIP_MUTATION" == "false" ]]; then
        log_stage "Mutation: Running MutationSmith..."
        start_timer

        # Stream output in real-time while capturing it
        local mutation_tmpfile=$(mktemp)
        set +e
        docker compose run --rm mutation python scripts/run_mutation_testing.py --spec "$SPEC_NAME" --spec-version "$version" 2>&1 | tee "$mutation_tmpfile"
        local mutation_exit=${PIPESTATUS[0]}
        set -e
        local mutation_output=$(cat "$mutation_tmpfile")
        rm -f "$mutation_tmpfile"

        METRIC_mutation_time=$(get_elapsed)

        # Parse mutation testing output
        # Format: "Total mutations: N", "Activated: N", "Killed: N", "Survived: N", "Mutation Score: X%"
        METRIC_mutation_total=$(echo "$mutation_output" | grep -E "^Total mutations:" | sed 's/[^0-9]//g' | head -1)
        METRIC_mutation_activated=$(echo "$mutation_output" | grep -E "^Activated:" | sed 's/[^0-9]//g' | head -1)
        METRIC_mutation_killed=$(echo "$mutation_output" | grep -E "^Killed:" | sed 's/[^0-9]//g' | head -1)
        METRIC_mutation_survived=$(echo "$mutation_output" | grep -E "^Survived:" | sed 's/[^0-9]//g' | head -1)
        METRIC_mutation_score=$(echo "$mutation_output" | grep -E "Mutation Score:" | grep -oE "[0-9]+\.[0-9]+" | head -1)

        # Default to 0 if parsing fails
        METRIC_mutation_total=${METRIC_mutation_total:-0}
        METRIC_mutation_activated=${METRIC_mutation_activated:-0}
        METRIC_mutation_killed=${METRIC_mutation_killed:-0}
        METRIC_mutation_survived=${METRIC_mutation_survived:-0}
        METRIC_mutation_score=${METRIC_mutation_score:-0}

        if [[ $mutation_exit -eq 0 ]]; then
            STAGE_mutation="pass"
            log_success "mutation completed in ${METRIC_mutation_time}s"
        else
            # Surviving mutants is expected (shows test suite gaps)
            if [[ ${METRIC_mutation_total} -gt 0 ]]; then
                STAGE_mutation="pass"
                log_success "mutation completed in ${METRIC_mutation_time}s"
            else
                STAGE_mutation="fail"
                log_error "mutation failed (exit code: $mutation_exit)"
            fi
        fi

        log_info "MS: ${METRIC_mutation_killed}/${METRIC_mutation_activated} killed (${METRIC_mutation_score}%)"
        if [[ ${METRIC_mutation_survived} -gt 0 ]]; then
            log_warn "${METRIC_mutation_survived} mutant(s) survived - indicates test suite gaps"
        fi
    else
        log_info "Skipping mutation testing (--skip-mutation)"
    fi

    # ═══════════════════════════════════════════════════════════════════
    # SURS (for v2 specs only)
    # ═══════════════════════════════════════════════════════════════════
    if [[ "$IS_V2" == "true" ]]; then
        log_stage "SURS: Running $SURS_SERVICE..."
        start_timer

        # Stream output in real-time while capturing it
        local surs_tmpfile=$(mktemp)
        set +e
        docker compose run --rm "$SURS_SERVICE" 2>&1 | tee "$surs_tmpfile"
        local surs_exit=${PIPESTATUS[0]}
        set -e
        local surs_output=$(cat "$surs_tmpfile")
        rm -f "$surs_tmpfile"

        METRIC_surs_time=$(get_elapsed)

        if [[ $surs_exit -eq 0 ]]; then
            STAGE_evaluate_surs="pass"
            read -r surs_passed surs_failed <<< $(parse_pytest_summary "$surs_output")
            METRIC_surs_passed=$surs_passed
            METRIC_surs_total=$((surs_passed + surs_failed))
            log_success "$SURS_SERVICE completed in ${METRIC_surs_time}s"
        else
            read -r surs_passed surs_failed <<< $(parse_pytest_summary "$surs_output")
            METRIC_surs_passed=$surs_passed
            METRIC_surs_total=$((surs_passed + surs_failed))

            if [[ ${METRIC_surs_total} -gt 0 ]]; then
                STAGE_evaluate_surs="pass"
                log_success "$SURS_SERVICE completed in ${METRIC_surs_time}s"
            else
                STAGE_evaluate_surs="fail"
                log_error "$SURS_SERVICE failed"
            fi
        fi

        local surs_percent=0
        if [[ ${METRIC_surs_total} -gt 0 ]]; then
            surs_percent=$(echo "scale=1; ${METRIC_surs_passed} * 100 / ${METRIC_surs_total}" | bc)
        fi
        log_info "SURS: ${METRIC_surs_passed}/${METRIC_surs_total} ($surs_percent%)"
    fi

    # Save results for this version
    save_version_results "$version"

    return 0
}

# Main pipeline execution
main() {
    local PIPELINE_START=$(date +%s)

    log_header "TDAD Full Pipeline"
    log_info "Run ID: $RUN_ID"
    log_info "Date: $RUN_DATE $RUN_TIME"
    log_info "Spec: $SPEC_NAME"
    log_info "Versions: $VERSIONS_TO_RUN"
    echo ""

    # Change to repo root
    cd "$REPO_ROOT"

    # ═══════════════════════════════════════════════════════════════════
    # Stage 0: Build Docker Images (all versions at once)
    # ═══════════════════════════════════════════════════════════════════
    log_header "Stage 0: Build Docker Images"

    # Build all services needed for all versions in one go
    # Always include mutation service to ensure it has latest code
    local services_to_build="init-volumes mutation"

    # Determine service suffix based on spec name
    local spec_suffix=""
    if [[ "$SPEC_NAME" != "supportops" ]]; then
        spec_suffix="-${SPEC_NAME}"
    fi

    for ver in $VERSIONS_TO_RUN; do
        if [[ "$ver" == "v1" ]]; then
            services_to_build="$services_to_build testsmith${spec_suffix} compiler${spec_suffix} evaluate${spec_suffix}"
        else
            services_to_build="$services_to_build testsmith${spec_suffix}-v2 compiler${spec_suffix}-v2 evaluate${spec_suffix}-v2 evaluate${spec_suffix}-surs"
        fi
    done

    log_stage "Building images: $services_to_build"
    start_timer

    # Build all services needed for this run
    if docker compose build $services_to_build; then
        log_success "Docker images built in $(get_elapsed)s"
    else
        log_error "Docker build failed"
        exit 1
    fi

    # ═══════════════════════════════════════════════════════════════════
    # Stage 1: Init Volumes (once for all versions)
    # ═══════════════════════════════════════════════════════════════════
    if [[ "$SKIP_INIT" == "false" ]]; then
        log_header "Stage 1: Initialize Volumes"
        log_stage "Running init-volumes..."
        start_timer

        if docker compose run --rm init-volumes; then
            STAGE_init_volumes="pass"
            log_success "init-volumes completed in $(get_elapsed)s"
        else
            STAGE_init_volumes="fail"
            log_error "init-volumes failed"
            # Continue anyway - volumes might already be initialized
        fi
    else
        log_info "Skipping init-volumes (--skip-init)"
    fi

    # ═══════════════════════════════════════════════════════════════════
    # Run pipeline for each version
    # ═══════════════════════════════════════════════════════════════════
    local all_passed=true
    for version in $VERSIONS_TO_RUN; do
        if ! run_version_pipeline "$version"; then
            all_passed=false
            log_error "Pipeline failed for $SPEC_NAME/$version"
        fi
    done

    # ═══════════════════════════════════════════════════════════════════
    # Final Summary
    # ═══════════════════════════════════════════════════════════════════
    local PIPELINE_END=$(date +%s)
    local TOTAL_TIME=$((PIPELINE_END - PIPELINE_START))

    log_header "Full Pipeline Complete"
    log_info "Total time: ${TOTAL_TIME}s"
    log_info "Results saved to $RESULTS_FILE"

    if [[ "$all_passed" == "true" ]]; then
        log_success "All versions completed successfully!"
    else
        log_error "Some versions had failures"
        exit 1
    fi
}

# Save results for a single version
save_version_results() {
    local version="$1"
    local version_run_id="${RUN_ID}_${version}"

    # Calculate percentages (format to ensure valid JSON with leading zero)
    local vpr_percent=0
    if [[ ${METRIC_vpr_total} -gt 0 ]]; then
        vpr_percent=$(format_decimal "$(echo "scale=1; ${METRIC_vpr_passed} * 100 / ${METRIC_vpr_total}" | bc)" 1)
    fi

    local hpr_percent=0
    if [[ ${METRIC_hpr_total} -gt 0 ]]; then
        hpr_percent=$(format_decimal "$(echo "scale=1; ${METRIC_hpr_passed} * 100 / ${METRIC_hpr_total}" | bc)" 1)
    fi

    local surs_percent=0
    if [[ ${METRIC_surs_total} -gt 0 ]]; then
        surs_percent=$(format_decimal "$(echo "scale=1; ${METRIC_surs_passed} * 100 / ${METRIC_surs_total}" | bc)" 1)
    fi

    # Calculate total costs for this version (TestSmith + Compiler + Test execution)
    local total_input_tokens=$((METRIC_testsmith_input_tokens + METRIC_compiler_input_tokens + METRIC_test_input_tokens))
    local total_cache_creation_tokens=$((METRIC_testsmith_cache_creation_tokens + METRIC_compiler_cache_creation_tokens + METRIC_test_cache_creation_tokens))
    local total_cache_read_tokens=$((METRIC_testsmith_cache_read_tokens + METRIC_compiler_cache_read_tokens + METRIC_test_cache_read_tokens))
    local total_output_tokens=$((METRIC_testsmith_output_tokens + METRIC_compiler_output_tokens + METRIC_test_output_tokens))
    local total_cost_usd=$(format_decimal "$(echo "scale=6; ${METRIC_testsmith_cost_usd} + ${METRIC_compiler_cost_usd} + ${METRIC_test_cost_usd}" | bc)" 6)
    local total_time=$((METRIC_testsmith_time + METRIC_compiler_time + METRIC_evaluate_time + METRIC_mutation_time + METRIC_surs_time))
    # Total input including all cache types
    local total_all_input=$((total_input_tokens + total_cache_creation_tokens + total_cache_read_tokens))

    # Print summary for this version
    log_header "Results: $SPEC_NAME/$version"

    echo -e "${CYAN}Metrics:${NC}"
    printf "  %-20s %s/%s (%s%%)\n" "VPR (Visible):" "${METRIC_vpr_passed}" "${METRIC_vpr_total}" "$vpr_percent"
    printf "  %-20s %s/%s (%s%%)\n" "HPR (Hidden):" "${METRIC_hpr_passed}" "${METRIC_hpr_total}" "$hpr_percent"
    printf "  %-20s %s/%s killed (%s%%)\n" "MS (Mutation):" "${METRIC_mutation_killed}" "${METRIC_mutation_activated}" "${METRIC_mutation_score}"
    if [[ "$IS_V2" == "true" ]]; then
        printf "  %-20s %s/%s (%s%%)\n" "SURS (Regression):" "${METRIC_surs_passed}" "${METRIC_surs_total}" "$surs_percent"
    fi
    printf "  %-20s %s\n" "Compiler Iterations:" "${METRIC_compiler_iterations}"
    printf "  %-20s %s visible, %s hidden\n" "Generated Tests:" "${METRIC_testsmith_visible_files}" "${METRIC_testsmith_hidden_files}"
    echo ""

    echo -e "${CYAN}API Costs:${NC}"
    local ts_all_in=$((METRIC_testsmith_input_tokens + METRIC_testsmith_cache_creation_tokens + METRIC_testsmith_cache_read_tokens))
    local cmp_all_in=$((METRIC_compiler_input_tokens + METRIC_compiler_cache_creation_tokens + METRIC_compiler_cache_read_tokens))
    local test_all_in=$((METRIC_test_input_tokens + METRIC_test_cache_creation_tokens + METRIC_test_cache_read_tokens))
    printf "  %-20s \$%s USD\n" "TestSmith:" "${METRIC_testsmith_cost_usd}"
    printf "    %-18s %s uncached, %s cache-write, %s cache-read\n" "Input:" "${METRIC_testsmith_input_tokens}" "${METRIC_testsmith_cache_creation_tokens}" "${METRIC_testsmith_cache_read_tokens}"
    printf "    %-18s %s\n" "Output:" "${METRIC_testsmith_output_tokens}"
    printf "  %-20s \$%s USD\n" "Compiler:" "${METRIC_compiler_cost_usd}"
    printf "    %-18s %s uncached, %s cache-write, %s cache-read\n" "Input:" "${METRIC_compiler_input_tokens}" "${METRIC_compiler_cache_creation_tokens}" "${METRIC_compiler_cache_read_tokens}"
    printf "    %-18s %s\n" "Output:" "${METRIC_compiler_output_tokens}"
    printf "  %-20s \$%s USD (%s tests)\n" "Test Execution:" "${METRIC_test_cost_usd}" "${METRIC_test_num_tests}"
    printf "    %-18s %s uncached, %s cache-write, %s cache-read\n" "Input:" "${METRIC_test_input_tokens}" "${METRIC_test_cache_creation_tokens}" "${METRIC_test_cache_read_tokens}"
    printf "    %-18s %s\n" "Output:" "${METRIC_test_output_tokens}"
    printf "  %-20s \$%s USD (%s total tokens)\n" "Total:" "$total_cost_usd" "$((total_all_input + total_output_tokens))"
    echo ""

    echo -e "${CYAN}Timing:${NC}"
    printf "  %-15s %ss\n" "TestSmith:" "${METRIC_testsmith_time}"
    printf "  %-15s %ss\n" "Compiler:" "${METRIC_compiler_time}"
    printf "  %-15s %ss\n" "Evaluate:" "${METRIC_evaluate_time}"
    printf "  %-15s %ss\n" "Mutation:" "${METRIC_mutation_time}"
    if [[ "$IS_V2" == "true" ]]; then
        printf "  %-15s %ss\n" "SURS:" "${METRIC_surs_time}"
    fi
    printf "  %-15s %ss\n" "Version Total:" "$total_time"
    echo ""

    # Build JSON result
    local json_result
    json_result=$(cat <<EOF
{
  "run_id": "$version_run_id",
  "date": "$RUN_DATE",
  "time": "$RUN_TIME",
  "spec": "$SPEC_NAME",
  "version": "$version",
  "stages": {
    "init_volumes": "${STAGE_init_volumes}",
    "testsmith": "${STAGE_testsmith}",
    "compiler": "${STAGE_compiler}",
    "evaluate": "${STAGE_evaluate}",
    "mutation": "${STAGE_mutation}",
    "evaluate_surs": "${STAGE_evaluate_surs}"
  },
  "metrics": {
    "seed_vpr_passed": ${METRIC_seed_vpr_passed:-0},
    "seed_vpr_total": ${METRIC_seed_vpr_total:-0},
    "seed_hpr_passed": ${METRIC_seed_hpr_passed:-0},
    "seed_hpr_total": ${METRIC_seed_hpr_total:-0},
    "vpr_passed": ${METRIC_vpr_passed},
    "vpr_total": ${METRIC_vpr_total},
    "vpr_percent": $vpr_percent,
    "hpr_passed": ${METRIC_hpr_passed},
    "hpr_total": ${METRIC_hpr_total},
    "hpr_percent": $hpr_percent,
    "mutation_total": ${METRIC_mutation_total},
    "mutation_activated": ${METRIC_mutation_activated},
    "mutation_killed": ${METRIC_mutation_killed},
    "mutation_survived": ${METRIC_mutation_survived},
    "mutation_score": ${METRIC_mutation_score:-0},
    "surs_passed": ${METRIC_surs_passed},
    "surs_total": ${METRIC_surs_total},
    "surs_percent": ${surs_percent:-0},
    "compiler_iterations": ${METRIC_compiler_iterations},
    "testsmith_visible_files": ${METRIC_testsmith_visible_files},
    "testsmith_hidden_files": ${METRIC_testsmith_hidden_files}
  },
  "timing": {
    "testsmith_seconds": ${METRIC_testsmith_time},
    "compiler_seconds": ${METRIC_compiler_time},
    "evaluate_seconds": ${METRIC_evaluate_time},
    "mutation_seconds": ${METRIC_mutation_time},
    "surs_seconds": ${METRIC_surs_time},
    "total_seconds": $total_time
  },
  "costs": {
    "testsmith_cost_usd": ${METRIC_testsmith_cost_usd},
    "testsmith_input_tokens": ${METRIC_testsmith_input_tokens},
    "testsmith_cache_creation_tokens": ${METRIC_testsmith_cache_creation_tokens},
    "testsmith_cache_read_tokens": ${METRIC_testsmith_cache_read_tokens},
    "testsmith_output_tokens": ${METRIC_testsmith_output_tokens},
    "compiler_cost_usd": ${METRIC_compiler_cost_usd},
    "compiler_input_tokens": ${METRIC_compiler_input_tokens},
    "compiler_cache_creation_tokens": ${METRIC_compiler_cache_creation_tokens},
    "compiler_cache_read_tokens": ${METRIC_compiler_cache_read_tokens},
    "compiler_output_tokens": ${METRIC_compiler_output_tokens},
    "test_cost_usd": ${METRIC_test_cost_usd},
    "test_input_tokens": ${METRIC_test_input_tokens},
    "test_cache_creation_tokens": ${METRIC_test_cache_creation_tokens},
    "test_cache_read_tokens": ${METRIC_test_cache_read_tokens},
    "test_output_tokens": ${METRIC_test_output_tokens},
    "test_num_tests": ${METRIC_test_num_tests},
    "total_cost_usd": $total_cost_usd,
    "total_input_tokens": $total_input_tokens,
    "total_cache_creation_tokens": $total_cache_creation_tokens,
    "total_cache_read_tokens": $total_cache_read_tokens,
    "total_output_tokens": $total_output_tokens
  }
}
EOF
)

    # Append to results file (create array if doesn't exist)
    if [[ -f "$RESULTS_FILE" ]]; then
        local temp_file=$(mktemp)
        jq ". += [$json_result]" "$RESULTS_FILE" > "$temp_file" && mv "$temp_file" "$RESULTS_FILE"
    else
        echo "[$json_result]" > "$RESULTS_FILE"
    fi

    # Also save individual version result to results/ folder
    # Naming: {spec}_{version}_{timestamp}.json (e.g., supportops_v1_20260127_143000.json)
    local version_result_file="$RESULTS_DIR/${SPEC_NAME}_${version}_${RUN_ID}.json"
    echo "$json_result" > "$version_result_file"
    log_info "Results saved: $version_result_file"
}

# Run main
main "$@"
