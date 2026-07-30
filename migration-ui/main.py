from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

import subprocess
import json
import threading
import time
import os
import logging


app = FastAPI()

logger = logging.getLogger("migration-experiment-runner")
logging.basicConfig(
    filename='./logs/server.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


LOG_FILE="logs/experiment.log"


process=None



class ExperimentConfig(BaseModel):

    predicates:dict

    governance_mode:str

    governance_thresholds:dict

    runtime:dict

    ranking_weights:dict

    ranked_services:list





def run_process(config):

    global process


    os.makedirs(
        "logs",
        exist_ok=True
    )


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



    process=subprocess.Popen(

        [
            "python3",
            "run_experiment.py",
            "experiment_config.json"
        ],

        stdout=subprocess.PIPE,

        stderr=subprocess.STDOUT,

        text=True

    )



    with open(
        LOG_FILE,
        "a"
    ) as log:


        for line in process.stdout:

            logger.info(line)
            log.write(line)
            log.flush()



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



@app.get("/logs")
def get_logs():

    if not os.path.exists(LOG_FILE):

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
        


app.mount(
    "/",
    StaticFiles(
        directory="static",
        html=True
    ),
    name="static"
)
