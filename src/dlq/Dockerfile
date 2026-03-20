FROM public.ecr.aws/lambda/python:3.13

COPY src/dlq/dlq_processor_requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/ ${LAMBDA_TASK_ROOT}/src/

CMD ["src.handlers.dlq_handler.handler"]
