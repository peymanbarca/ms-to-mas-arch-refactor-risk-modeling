import json
import sys
import time
import logging


logger = logging.getLogger("migration-experiment-runner")
logging.basicConfig(
    filename='./logs/experiment.log',
    level=logging.INFO,  # Log all messages with severity DEBUG or higher
    format='%(asctime)s - %(levelname)s - %(message)s'  # Define the message format
)


logger.info("========== Migration Started ==========")


logger.info(
    f"Selected Predicates: config['predicates']"
)



logger.info(
    f"Governance: config['governance_mode']"
)



logger.info(
    f"LLM: config['runtime']['model']",
)


logger.info(
    f"Temperature: config['runtime']['temperature']"
)


logger.info(
    f"Requests: {config['runtime']['R']}"
)


logger.info(
    f"Concurrency: {config['runtime']['concurrency']}"
)




for i,service in enumerate(
    config["ranked_services"]
):

    logger.info(
        f"STEP {i+1}. {service}"
    )
    
    logger.info(
        f"[STEP {i+1}] Migrating service {service['service']} ..."
    )

    time.sleep(0.5)




logger.info(
    "========== Migration Completed =========="
)