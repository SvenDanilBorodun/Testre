FROM robotis/open-manipulator:amd64-4.1.4

# Install Python dynamixel-sdk for identify_arm.py
# Install v4l-utils for camera device discovery (v4l2-ctl --info)
RUN apt-get update && apt-get install -y --no-install-recommends \
        v4l-utils python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages dynamixel-sdk==4.0.3

# Copy entrypoint and identification script
COPY entrypoint_omx.sh /entrypoint_omx.sh
COPY identify_arm.py /usr/local/bin/identify_arm.py
# Strip Windows CRLF line endings (Git on Windows may convert LF→CRLF)
RUN sed -i 's/\r$//' /entrypoint_omx.sh /usr/local/bin/identify_arm.py \
    && chmod +x /entrypoint_omx.sh /usr/local/bin/identify_arm.py

ENTRYPOINT ["/entrypoint_omx.sh"]
