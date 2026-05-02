FROM public.ecr.aws/lambda/python:3.13

COPY src/transfer/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY src/ ${LAMBDA_TASK_ROOT}/src/

CMD ["src.handlers.wizard_transfer_handler.handler"]
