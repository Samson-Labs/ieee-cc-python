FROM public.ecr.aws/lambda/python:3.12

COPY src/generators/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Bundle text overlay fonts. Each family lives in its own directory under
# /usr/share/fonts/truetype/, matching the standard Linux distro layout
# expected by image_overlay_generator._FONT_FAMILY_PATHS.
COPY src/generators/fonts/opensans/      /usr/share/fonts/truetype/opensans/
COPY src/generators/fonts/roboto/        /usr/share/fonts/truetype/roboto/
COPY src/generators/fonts/courierprime/  /usr/share/fonts/truetype/courierprime/

COPY src/ ${LAMBDA_TASK_ROOT}/src/

CMD ["src.handlers.image_overlay_handler.handler"]
