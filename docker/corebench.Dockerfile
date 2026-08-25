# Offline runtime for the frozen Asta CORE-Bench-Hard validation mini-suite.
#
# It contains public scientific dependencies only. Capsule repositories are mounted per task;
# hidden answers, evaluator code, and benchmark annotations are never copied into this image.
FROM aletheia-evaluator-agent:latest

RUN pip install --no-cache-dir \
    "networkx==3.6.1" \
    "seaborn==0.13.2" \
    "jupyter==1.1.1" \
    "nbconvert==7.17.0"
