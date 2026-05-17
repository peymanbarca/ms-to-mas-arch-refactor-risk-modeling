# Quick Start: SonarQube Analysis

## One-Command Setup (macOS/Linux)

```bash
# From project root
bash setup-sonarqube.sh
```

This script will:
1. ✅ Start SonarQube and database
2. ✅ Wait for SonarQube to be ready
3. ✅ Check for sonar-scanner installation
4. ✅ Run the analysis

## Manual Setup (Step by Step)

### 1. Start Services
```bash
docker-compose up -d sonarqube sonarqube-db
```

### 2. Wait for SonarQube (60 seconds)
```bash
# Check health
curl http://localhost:9000/api/system/health
```

### 3. Setup Token
- Open http://localhost:9000
- Login: `admin` / `admin`
- Admin → Security → Users → Generate Token

### 4. Run Analysis
```bash
sonar-scanner \
  -Dsonar.projectBaseDir=. \
  -Dsonar.host.url=http://localhost:9000 \
  -Dsonar.login=YOUR_TOKEN
```

### 5. View Results
- Open http://localhost:9000
- Click **ms-to-mas-retailben** project

## What Gets Analyzed

✅ All Python files in `ms_baseline/retailben/`
✅ Cyclomatic Complexity (decision points)
✅ Cognitive Complexity (code understandability)
✅ Code Duplications
✅ Code Smells & Issues

## Key Metrics

| Metric | Good | Acceptable | Poor |
|--------|------|------------|------|
| Cyclomatic Complexity per method | < 5 | < 10 | > 15 |
| Cognitive Complexity per method | < 5 | < 15 | > 25 |
| Code Duplication | < 5% | < 10% | > 15% |

## Troubleshooting

**SonarQube won't start:**
```bash
docker-compose logs sonarqube
```

**sonar-scanner not found:**
```bash
# macOS
brew install sonar-scanner

# Ubuntu/Debian
sudo apt-get install sonar-scanner
```

**Connection refused:**
```bash
# Wait longer (60+ seconds)
sleep 30 && curl http://localhost:9000/api/system/health
```

## Analyze Specific Service

Edit `sonar-project.properties` and set:
```properties
sonar.sources=ms_baseline/retailben/inventory_service.py
sonar.projectKey=inventory-service
sonar.projectName=Inventory Service
```

Then run sonar-scanner again.

---

📖 For detailed info: See `SONARQUBE_SETUP.md`
