from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates

import subprocess
import json
import threading
import time
import os
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI()

templates = Jinja2Templates(
    directory=str(BASE_DIR) +  "/migration-ui" + "/templates"
)

logger = logging.getLogger("migration-experiment-runner")
logging.basicConfig(
    filename='./logs/server.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


# LOG_FILE="logs/experiment.log"
LOG_FILE = str(BASE_DIR) + '/deploy_orchestration/retailben/logs/experiment.log'

process=None

class ExperimentInput(BaseModel):

    response: str

class ExperimentConfig(BaseModel):

    predicates:dict

    governance_mode:str

    governance_thresholds:dict

    runtime:dict

    ranking_weights:dict

    ranked_services:list


experiment_process = None
experiment_lock = threading.Lock()


def run_process(config):

    global process
    global experiment_process


    # os.makedirs(
    #     "logs",
    #     exist_ok=True
    # )


    with open(
        "experiment_config.json",
        "w"
    ) as f:

        json.dump(
            config,
            f,
            indent=4
        )



    open(
        LOG_FILE,
        "w"
    ).close()

    logger.info("previous log file trimmed.")



    # process=subprocess.Popen(

    #     [
    #         "python3",
    #         "run_experiment_sample.py",
    #         "experiment_config.json"
    #     ],

    #     stdout=subprocess.PIPE,

    #     stderr=subprocess.STDOUT,

    #     text=True

    # )



    # with open(
    #     LOG_FILE,
    #     "a"
    # ) as log:


    #     for line in process.stdout:

    #         logger.info(line)
    #         log.write(line)
    #         log.flush()


    config_json = json.dumps(config)
    logger.info(f"Calling str(BASE_DIR) + '/deploy_orchestration/retailben/live_progressive_refactor_orchestrator.py' with config: {config_json} ")
    
    process=subprocess.Popen(

        [
            "python3",
            str(BASE_DIR) + '/deploy_orchestration/retailben/live_progressive_refactor_orchestrator.py',
            config_json
        ],

        stdin=subprocess.PIPE,          # IMPORTANT

        stdout=subprocess.PIPE,

        stderr=subprocess.STDOUT,

        text=True,
        bufsize=1,
        
        cwd=str(BASE_DIR) + "/deploy_orchestration/retailben",

    )
    

    with experiment_lock:

        experiment_process = process


    # Read stdout continuously

    for line in process.stdout:

        logger.info(
            line.rstrip()
        )


    process.wait()


    with experiment_lock:

        experiment_process = None

@app.post("/run-experiment")
def run_experiment(config:ExperimentConfig):

    thread=threading.Thread(

        target=run_process,

        args=(config.dict(),)

    )


    thread.start()
    logger.info("run_experiment thread started.")


    return {

        "status":"started"

    }


@app.post("/experiment-input")
def experiment_input(data: ExperimentInput):

    global experiment_process

    response = data.response.strip().upper()


    if response not in ["A", "R", "H"]:

        return {
            "status": "error",
            "message": "Invalid response. Use A, R, or H."
        }


    with experiment_lock:

        process = experiment_process


    if process is None:

        return {
            "status": "error",
            "message": "No experiment is currently running."
        }


    if process.poll() is not None:

        return {
            "status": "error",
            "message": "Experiment has already finished."
        }


    try:

        process.stdin.write(
            response + "\n"
        )

        process.stdin.flush()


        logger.info(
            "HITL response sent to experiment: %s",
            response
        )


        return {
            "status": "accepted",
            "response": response
        }


    except Exception as e:

        logger.exception(
            "Failed to send experiment input"
        )

        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/logs")
def get_logs():

    if not os.path.exists(LOG_FILE):
        logger.error("log file does not exist.")
        return {
            "logs":""
        }


    with open(
        LOG_FILE,
        "r"
    ) as f:

        return {

            "logs":f.read()

        }
        


@app.get("/")
def system_selection():

    return FileResponse(
        BASE_DIR / "migration-ui" /  "static" / "select_system.html"
    )
    

@app.get("/retailben")
async def retailben(request: Request):

    return templates.TemplateResponse(
        "benchmarks/retailben.html",
        {
            "request": request,
            "benchmark": "retailben"
        }
    )

app.mount(
    "/figures",
    StaticFiles(
        directory=str(BASE_DIR) + "/figures"
    ),
    name="figures"
)

app.mount(
    "/static",
    StaticFiles(
        directory=str(BASE_DIR) + "/migration-ui" + "/static"
    ),
    name="static"
)

app.mount(
    "/",
    StaticFiles(
        directory="static",
        html=True
    ),
    name="static"
)
