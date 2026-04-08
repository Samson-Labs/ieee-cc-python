FROM public.ecr.aws/lambda/python:3.13

COPY src/ai/requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/ai/ ${LAMBDA_TASK_ROOT}/src/ai/
COPY src/common/ ${LAMBDA_TASK_ROOT}/src/common/
COPY src/handlers/bedrock_handler.py ${LAMBDA_TASK_ROOT}/src/handlers/bedrock_handler.py
COPY src/__init__.py ${LAMBDA_TASK_ROOT}/src/__init__.py
COPY src/handlers/__init__.py ${LAMBDA_TASK_ROOT}/src/handlers/__init__.py

CMD ["src.handlers.bedrock_handler.handler"]
