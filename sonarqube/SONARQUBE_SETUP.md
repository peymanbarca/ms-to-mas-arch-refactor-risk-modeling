# SonarQube Setup Guide for MS-to-MAS Project

This guide walks through setting up SonarQube locally to analyze the cyclomatic and cognitive complexity of the microservices.

## Prerequisites

- Docker and Docker Compose installed
- `sonar-scanner` CLI tool (for running analysis)
- Python 3.8+ (for your microservices)

## Step 1: Start SonarQube with Docker Compose

```bash
# From the project root directory
docker compose up -d sonarqube sonarqube-db

# Wait for SonarQube to be fully ready (approximately 60 seconds)
# You can check status:
docker-compose logs -f sonarqube
```

SonarQube will be available at: **http://localhost:9000**

## Step 2: Initial SonarQube Setup

1. Open http://localhost:9000 in your browser
2. Login with default credentials:
   - **Username**: `admin`
   - **Password**: `admin`
3. You'll be prompted to change the password (set a new one) Admin@123456
4. Create a token for scanner authentication:
   - Go to **Administration** → **Security** → **Users** → Click on your user
   - Generate a token (or use the default admin token for local development)
     - retailben: sqp_41fe3045b00175913d43048d9844410f700e2caa
     - google_ms: sqp_36b259cb74c6c79705b900f51af6c8242dd88d53

   - Copy the token (you'll need it in Step 4)

## Step 3: Install SonarQube Scanner

### Option A: Using Homebrew (macOS)
```bash
brew install sonar-scanner
```

### Option B: Using apt (Ubuntu/Debian)
```bash
sudo apt-get install -y sonar-scanner
```

### Option C: Manual Download
```bash
# Download from: https://docs.sonarqube.org/latest/analyzing-source-code/scanners/sonarscanner/
# Then add to PATH
export PATH="/path/to/sonar-scanner/bin:$PATH"
```

### Verify Installation
```bash
sonar-scanner --version
```

## Step 4: Configure and Run Analysis

### Update sonar-project.properties

The `sonar-project.properties` file is already created in the project root with the following key settings:
- `sonar.projectKey=ms-to-mas-retailben`
- `sonar.sources=ms_baseline/retailben` (analyze retailben microservices)
- `sonar.language=py` (Python)
- Excludes: `__pycache__`, tests, virtual environments

### Run the SonarQube Analysis

```bash
# Navigate to project root
cd /home/ghazal/PhD/impl/ms-to-mas-arch-refactor-risk-modeling

# Run analysis (replace TOKEN with your generated token)
sonar-scanner \
  -Dsonar.projectBaseDir=. \
  -Dsonar.host.url=http://localhost:9000 \
  -Dsonar.login=YOUR_TOKEN_HERE

# run with pysonar: retailben
pysonar \
  --sonar-host-url=http://localhost:9000 \
  --sonar-token=sqp_41fe3045b00175913d43048d9844410f700e2caa \
  --sonar-project-key=retailben

# run with pysonar: google_ms

pysonar \
  --sonar-host-url=http://localhost:9000 \
  --sonar-token=sqp_36b259cb74c6c79705b900f51af6c8242dd88d53 \
  --sonar-project-key=google-ms \
  --sonar-coverage-exclusions=**/shared/**,**/client.py


# run with pysonar: dsb_social

pysonar \
  --sonar-host-url=http://localhost:9000 \
  --sonar-token=sqp_9d694f864c105c184c06f58f0b1921b15c0bb65f \
  --sonar-project-key=dsb_social \
  --sonar-coverage-exclusions=**/gen_py/**,**/source_code/**,**/client.py

## Step 5: View Results

After the scan completes (2-5 minutes depending on code size):

1. Open http://localhost:9000
2. Click on the **ms-to-mas-retailben** project
3. Review metrics:
   - **Cyclomatic Complexity**: Under "Complexity" section
   - **Cognitive Complexity**: Under "Code Smells" → "Cognitive Complexity"
   - **Duplication**: Copy/paste detection
   - **Code Coverage**: If coverage reports are provided

## Key Metrics to Monitor

| Metric | Description | Target |
|--------|-------------|--------|
| **Cyclomatic Complexity** | Number of decision points in code | < 10 per method |
| **Cognitive Complexity** | How difficult code is to understand | < 15 per method |
| **Code Duplications** | Percentage of duplicated lines | < 10% |
| **Code Coverage** | Test coverage percentage | > 70% |
| **Issues** | Bugs, vulnerabilities, code smells | Minimize |

## Analyzing Specific Modules

To analyze specific microservices separately, modify `sonar-project.properties`:

```properties
# For Order Service only
sonar.sources=ms_baseline/retailben/order_service.py

# For all services in a specific pattern
sonar.sources=ms_baseline/retailben/*_service.py
```

Then run:
```bash
sonar-scanner \
  -Dsonar.projectKey=order-service \
  -Dsonar.projectName="Order Service" \
  -Dsonar.host.url=http://localhost:9000 \
  -Dsonar.login=YOUR_TOKEN
```

## Adding Python Coverage Reports (Optional)

To include test coverage in analysis:

1. Install coverage tools:
```bash
pip install pytest pytest-cov
```

2. Run tests with coverage:
```bash
pytest --cov=ms_baseline/retailben --cov-report=xml
```

3. Update sonar-project.properties:
```properties
sonar.python.coverage.reportPaths=coverage.xml
```

4. Re-run analysis

## Troubleshooting

### SonarQube won't start
```bash
# Check logs
docker-compose logs sonarqube

# Increase VM memory
# Add to docker-compose.yaml in sonarqube service:
# environment:
#   _JAVA_OPTIONS: "-Xmx1024m -Xms512m"
```

### Scanner connection error
```bash
# Verify SonarQube is running:
curl http://localhost:9000/api/system/health

# Check token validity
# Re-generate token in SonarQube UI
```

### No Python files detected
```bash
# Verify sonar.sources path is correct (relative to project root)
# Check that .py files exist in the specified directory
ls -la ms_baseline/retailben/
```

## Useful Commands

```bash
# View SonarQube logs
docker-compose logs -f sonarqube

# Stop all services
docker-compose down

# Stop only SonarQube
docker-compose stop sonarqube

# Clean up SonarQube data
docker-compose down -v sonarqube sonarqube-db

# Check SonarQube health
curl http://localhost:9000/api/system/health
```

## Next Steps

1. **Set Quality Gates**: Define code quality standards in SonarQube
2. **Enable Rules**: Configure Python-specific rules for your analysis
3. **Integrate with CI/CD**: Add sonar-scanner to your pipeline
4. **Track Trends**: Monitor metrics over time using SonarQube dashboards
5. **Code Reviews**: Use findings to guide refactoring efforts

## Resources

- [SonarQube Documentation](https://docs.sonarqube.org/)
- [Python Plugin](https://docs.sonarqube.org/latest/analyzing-source-code/languages/python/)
- [SonarScanner Guide](https://docs.sonarqube.org/latest/analyzing-source-code/scanners/sonarscanner/)
- [Quality Gates](https://docs.sonarqube.org/latest/user-guide/quality-gates/)
