FROM public.ecr.aws/lambda/python:3.12

COPY src/generators/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Bundle OpenSans fonts from the existing image-generator Lambda
COPY src/generators/fonts/ /usr/share/fonts/truetype/opensans/

COPY src/ ${LAMBDA_TASK_ROOT}/src/

CMD ["src.handlers.image_overlay_handler.handler"]
