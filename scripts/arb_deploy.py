#!/usr/bin/env python3

# Copyright 2019, Offchain Labs, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ----------------------------------------------------------------------------
# arb-deploy
# ----------------------------------------------------------------------------

import argparse
import os
import sys
import shutil

import setup_states
from support.run import run

# package configuration
NAME = "arb-deploy"
DESCRIPTION = "Manage Arbitrum dockerized deployments"
ROOT_DIR = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# filename constants
DOCKER_COMPOSE_FILENAME = "docker-compose.yml"
VALIDATOR_STATE_DIRNAME = "validator-states/validator"

### ----------------------------------------------------------------------------
### docker-compose template
### ----------------------------------------------------------------------------

# Parameters: number of validators,
# absolute path to state folder, absolute path to contract
COMPOSE_HEADER = """# Machine generated by `arb-deploy`. Do not version control.
version: '3'
services:
    dockerhost:
        image: qoomon/docker-host
        cap_add: [ 'NET_ADMIN', 'NET_RAW' ]
        restart: on-failure

    arb-validator-coordinator:
        depends_on:
            - dockerhost
        volumes:
            - %s:/home/user/state
            - %s:/home/user/contract.ao
        image: arb-validator
        build:
            context: %s
            dockerfile: %s
        environment:
            - ID=0
            - WAIT_FOR=dockerhost:7546
            - ETH_URL=ws://dockerhost:7546
            - AVM=%s
        ports:
            - '1235:1235'
            - '1236:1236'
"""


def compose_header(state_abspath, contract_abspath, cvalidator, dockerfile, avm):
    return COMPOSE_HEADER % (
        state_abspath,
        contract_abspath,
        cvalidator,
        dockerfile,
        avm,
    )


# Parameters: validator id, absolute path to state folder,
# absolute path to contract, validator id
COMPOSE_VALIDATOR = """
    arb-validator%d:
        depends_on:
            - dockerhost
        volumes:
            - %s:/home/user/state
            - %s:/home/user/contract.ao
        image: arb-validator
        environment:
            - ID=%d
            - WAIT_FOR=arb-validator-coordinator:1236
            - ETH_URL=ws://dockerhost:7546
            - COORDINATOR_URL=wss://arb-validator-coordinator:1236/ws
            - AVM=%s
"""


# Returns one arb-validator declaration for a docker compose file
def compose_validator(validator_id, state_abspath, contract_abspath, avm):
    return COMPOSE_VALIDATOR % (
        validator_id,
        state_abspath,
        contract_abspath,
        validator_id,
        avm,
    )


DOCKERFILE_CACHE = """FROM alpine:3.9
RUN mkdir /build /cpp-build /rocksdb
FROM scratch
COPY --from=0 /cpp-build /cpp-build
COPY --from=0 /rocksdb /rocksdb
COPY --from=0 /build /build"""

### ----------------------------------------------------------------------------
### Deploy
### ----------------------------------------------------------------------------


# Compile contracts to `contract.ao` and export to Docker and run validators
def deploy(contract_name, n_validators, sudo_flag, build_flag, up_flag, avm):
    # Bootstrap the build cache if it does not exist
    def bootstrap_build_cache(name):
        if (
            run(
                "docker images -q %s" % name,
                capture_stdout=True,
                quiet=True,
                sudo=sudo_flag,
            )
            == ""
        ):
            run("mkdir -p .tmp")
            run('echo "%s" > .tmp/Dockerfile' % DOCKERFILE_CACHE)
            run("docker build -t %s .tmp" % name, sudo=sudo_flag)
            run("rm -rf .tmp")

    bootstrap_build_cache("arb-avm-cpp")
    bootstrap_build_cache("arb-validator")

    # Stop running Arbitrum containers
    halt_docker(sudo_flag)

    # Overwrite DOCKER_COMPOSE_FILENAME
    states_path = os.path.abspath(
        os.path.join(setup_states.VALIDATOR_STATES, setup_states.VALIDATOR_STATE)
    )
    compose = os.path.abspath("./" + DOCKER_COMPOSE_FILENAME)
    contract = os.path.abspath(contract_name)
    contents = compose_header(
        states_path % 0,
        contract,
        os.path.abspath(os.path.join(ROOT_DIR, "packages")),
        "arb-validator.Dockerfile",
        avm,
    ) + "".join(
        [
            compose_validator(i, states_path % i, contract, avm)
            for i in range(1, n_validators)
        ]
    )
    with open(compose, "w") as f:
        f.write(contents)

    # Build
    if not up_flag or build_flag:
        if run("docker-compose -f %s build" % compose, sudo=sudo_flag) != 0:
            exit(1)

    # Nuke old validator states if it exists
    if os.path.isdir(setup_states.VALIDATOR_STATES):
        shutil.rmtree(setup_states.VALIDATOR_STATES)

    # Setup validator states
    setup_states.setup_validator_states_ethbridge(
        os.path.abspath(contract_name), n_validators, sudo_flag
    )

    # if not os.path.isdir(setup_states.VALIDATOR_STATES):
    #     setup_states.setup_validator_states_ethbridge(
    #         os.path.abspath(contract_name), n_validators, sudo_flag
    #     )

    # Run
    if not build_flag or up_flag:
        run("docker-compose -f %s up" % compose, sudo=sudo_flag)


def halt_docker(sudo_flag):
    # Check for DOCKER_COMPOSE_FILENAME and halt if running
    if os.path.isfile("./" + DOCKER_COMPOSE_FILENAME):
        run(
            "docker-compose -f ./%s down -t 0" % DOCKER_COMPOSE_FILENAME,
            sudo=sudo_flag,
            capture_stdout=True,
        )

    # Kill and rm all docker containers and images created by any `arb-deploy`
    ps = "grep -e 'arb-validator' | awk '{ print $1 }'"
    if run("docker ps | " + ps, capture_stdout=True, quiet=True, sudo=sudo_flag) != "":
        run(
            "docker kill $("
            + ("sudo " if sudo_flag else "")
            + "docker ps | "
            + ps
            + ")",
            capture_stdout=True,
            sudo=sudo_flag,
        )
        run(
            "docker rm $("
            + ("sudo " if sudo_flag else "")
            + "docker ps -a | "
            + ps
            + ")",
            capture_stdout=True,
            sudo=sudo_flag,
        )


### ----------------------------------------------------------------------------
### Command line interface
### ----------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(prog=NAME, description=DESCRIPTION)
    # Required
    parser.add_argument("contract", help="The Arbitrum bytecode contract to deploy.")
    parser.add_argument(
        "n_validators", type=int, help="The number of validators to deploy."
    )
    # Optional
    parser.add_argument(
        "-s",
        "--sudo",
        action="store_true",
        dest="sudo",
        help="Run docker-compose with sudo",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--build",
        action="store_true",
        dest="build",
        help="Run docker-compose build only",
    )
    group.add_argument(
        "--up", action="store_true", dest="up", help="Run docker-compose up only"
    )

    parser.add_argument(
        "-a",
        "--avm",
        default="cpp",
        dest="avm",
        choices=["cpp", "go", "test"],
        help="Control the avm backend (default cpp)",
    )

    args = parser.parse_args()

    # Deploy
    deploy(args.contract, args.n_validators, args.sudo, args.build, args.up, args.avm)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
