import os
import logging
from functools import lru_cache
from flask import Flask, request, jsonify
import json
import boto3
from botocore.exceptions import ClientError
import datetime

# Selenium and WebDriver imports
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Logging configuration
# This sets up how our application will log information
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# AWS DynamoDB configuration
# Initialize connection to DynamoDB
try:
    # Connect directly to AWS DynamoDB
    dynamodb = boto3.resource(
        'dynamodb',
        region_name='ap-south-1',
        aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY')
    )
    logger.info("Connected to AWS DynamoDB")
except Exception as e:
    logger.error(f"Failed to connect to AWS DynamoDB: {str(e)}")
    raise

# Check if table exists and create it if needed
def ensure_table_exists():
    try:
        # Check if the table exists
        existing_tables = dynamodb.meta.client.list_tables()['TableNames']
        
        if 'transcribe' not in existing_tables:
            logger.info("Creating 'transcribe' table in AWS DynamoDB...")
            table = dynamodb.create_table(
                TableName='transcribe',
                KeySchema=[
                    {
                        'AttributeName': 'transcribeId',
                        'KeyType': 'HASH'  # Partition key
                    }
                ],
                AttributeDefinitions=[
                    {
                        'AttributeName': 'transcribeId',
                        'AttributeType': 'S'  # String type
                    }
                ],
                BillingMode='PAY_PER_REQUEST'  # On-demand capacity
                # Or use provisioned capacity:
                # ProvisionedThroughput={
                #     'ReadCapacityUnits': 5,
                #     'WriteCapacityUnits': 5
                # }
            )
            # Wait until the table exists
            table.meta.client.get_waiter('table_exists').wait(TableName='transcribe')
            logger.info("Table 'transcribe' created successfully in AWS!")
        else:
            logger.info("Table 'transcribe' already exists in AWS")
            
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            logger.info("Table already exists or is being created")
            return True
        else:
            logger.error(f"Error creating table: {str(e)}")
            return False

# Try to ensure the table exists
if ensure_table_exists():
    table = dynamodb.Table('transcribe')
else:
    logger.error("Failed to ensure table exists. Some operations may fail.")
    table = dynamodb.Table('transcribe')  # Still try to reference the table

class WebDriverPool:
    """
    This class manages a pool of Chrome WebDriver instances
    to efficiently handle multiple requests
    """
    _drivers = []  # Class variable to store WebDriver instances
    MAX_POOL_SIZE = 5  # Maximum number of drivers to keep in pool
    
    @classmethod
    def get_driver(cls):
        """
        Get a WebDriver from the pool or create a new one if needed
        """
        if len(cls._drivers) < cls.MAX_POOL_SIZE:
            try:
                # Create new driver with optimized settings
                options = cls._get_optimized_options()
                
                # In Docker Selenium image, Chrome is already set up correctly
                driver = webdriver.Chrome(options=options)
                
                cls._drivers.append(driver)
                logger.info("Successfully created a new WebDriver instance")
                return driver
            except Exception as e:
                logger.error(f"Failed to create WebDriver: {str(e)}")
                return None
        
        # Return first available driver if pool is full
        if cls._drivers:
            return cls._drivers.pop(0)
        else:
            logger.error("No WebDriver instances available in pool")
            return None
    
    @classmethod
    def release_driver(cls, driver):
        """
        Return a driver to the pool or close it if pool is full
        """
        if driver:
            if len(cls._drivers) < cls.MAX_POOL_SIZE:
                cls._drivers.append(driver)
            else:
                try:
                    driver.quit()
                except Exception as e:
                    logger.error(f"Error closing driver: {str(e)}")
    
    @staticmethod
    def _get_optimized_options():
        """
        Configure Chrome options for optimal performance in Docker environment
        """
        options = Options()
        options.add_argument("--headless")  # Run in headless mode (no GUI)
        options.add_argument("--no-sandbox")  # Required in Docker
        options.add_argument("--disable-dev-shm-usage")  # Required in Docker 
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920x1080")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-infobars")
        options.add_argument("--blink-settings=imagesEnabled=false")  # Disable images for speed
        options.page_load_strategy = 'eager'
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3")
        return options

# Create Flask application instance
app = Flask(__name__)

@lru_cache(maxsize=100)
def check_scheme_id(scheme_id):
    """
    Check scheme ID with caching and error handling
    """
    driver = None
    try:
        # Get a driver from the pool
        driver = WebDriverPool.get_driver()
        
        if not driver:
            logger.error("Failed to obtain WebDriver")
            return {"status": "error", "error": "Unable to initialize WebDriver"}
        
        # Navigate to the website
        logger.info(f"Checking scheme ID: {scheme_id}")
        driver.get("https://www.sspcrs.ie/portal/checker/pub/check")
        
        # Wait for and interact with webpage elements
        input_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "schemeIdInput"))
        )
        input_field.clear()
        input_field.send_keys(scheme_id)
        
        submit_button = driver.find_element(By.XPATH, "//button[@type='submit']")
        submit_button.click()
        
        # Wait for and get result
        card_element = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#page-content > div.main-box > div.pt-2 > div > div"))
        )
        
        result = card_element.text
        logger.info(f"Result obtained for scheme ID {scheme_id}")
        
        return {"status": "success", "result": result}
    
    except Exception as e:
        logger.error(f"Error checking scheme ID {scheme_id}: {e}")
        return {"status": "error", "error": str(e)}
    
    finally:
        # Always release the driver back to pool
        if driver:
            WebDriverPool.release_driver(driver)

@app.route('/check_status', methods=['POST'])
def check_status():
    """
    API endpoint to check scheme status
    """
    try:
        # Parse JSON data from request
        data = request.get_json()
        scheme_id = data.get('scheme_id')
        
        if not scheme_id:
            return jsonify({"error": "Scheme ID is required"}), 400
        
        # Check scheme ID and return result
        result = check_scheme_id(scheme_id)
        return jsonify(result), 200
    
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON"}), 400
    
    except Exception as e:
        logger.error(f"Unexpected error in check_status: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/save/transcribe', methods=['POST'])
def save_transcription():
    """
    API endpoint to save transcription data to DynamoDB
    """
    try:
        # Parse JSON data from request
        data = request.get_json()
        logger.info(f"Received data: {data}")
        
        # Validate required fields
        required_fields = ['transcribeId', 'doctorId', 'duration', 'transcribe']
        for field in required_fields:
            if field not in data:
                logger.error(f"Missing required field: {field}")
                return jsonify({"error": f"Missing required field: {field}"}), 400
        
        # Prepare item for DynamoDB with explicit type conversions
        try:
            item = {
                'transcribeId': str(data['transcribeId']),  # Convert to string
                'doctorId': str(data['doctorId']),          
                'duration': int(data['duration']),          
                'transcribe': str(data['transcribe']),      
                'timestamp': str(datetime.datetime.now())
            }
            
            # Add optional fields if they exist
            if 'notes' in data:
                item['notes'] = str(data['notes'])
                
            logger.info(f"Prepared item for DynamoDB: {item}")
        except ValueError as e:
            logger.error(f"Error converting data types: {str(e)}")
            return jsonify({"error": f"Data type error: {str(e)}"}), 400
            
        # Save to DynamoDB
        table.put_item(Item=item)
        
        logger.info(f"Transcription saved successfully: {item['transcribeId']}")
        
        return jsonify({
            "status": "success",
            "message": "Transcription saved successfully",
            "data": item
        }), 200
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"AWS DynamoDB error: {error_code} - {error_message}")
        
        # Handle specific AWS errors
        if error_code == 'ResourceNotFoundException':
            return jsonify({"error": "DynamoDB table not found. Please ensure the table exists."}), 500
        elif error_code == 'ProvisionedThroughputExceededException':
            return jsonify({"error": "DynamoDB throughput exceeded. Please try again later."}), 429
        elif error_code == 'AccessDeniedException':
            return jsonify({"error": "Access denied to DynamoDB. Check AWS credentials and permissions."}), 403
        else:
            return jsonify({"error": f"Database error: {error_message}"}), 500
    except json.JSONDecodeError:
        logger.error("Invalid JSON format")
        return jsonify({"error": "Invalid JSON format"}), 400
    except Exception as e:
        logger.error(f"Unexpected error in save_transcription: {str(e)}")
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# Cleanup function for WebDriver pool
def cleanup_drivers():
    """
    Cleanup WebDriver instances on application shutdown
    """
    for driver in WebDriverPool._drivers:
        try:
            driver.quit()
        except Exception as e:
            logger.error(f"Error closing driver: {e}")

# Register cleanup function to run on application exit
import atexit
atexit.register(cleanup_drivers)

# Health check endpoint for testing connectivity
@app.route('/health', methods=['GET'])
def health_check():
    """
    Simple health check endpoint
    """
    try:
        return jsonify({
            "status": "healthy",
            "service": "online"
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

if __name__ == '__main__':
    # If running with Python directly, log all the environment variables that might affect AWS
    if os.environ.get('AWS_ACCESS_KEY_ID'):
        logger.info("AWS_ACCESS_KEY_ID is set")
    else:
        logger.warning("AWS_ACCESS_KEY_ID is not set")
    
    if os.environ.get('AWS_SECRET_ACCESS_KEY'):
        logger.info("AWS_SECRET_ACCESS_KEY is set")
    else:
        logger.warning("AWS_SECRET_ACCESS_KEY is not set")
        
    if os.environ.get('AWS_DEFAULT_REGION'):
        logger.info(f"AWS_DEFAULT_REGION is set to {os.environ.get('AWS_DEFAULT_REGION')}")
    else:
        logger.warning("AWS_DEFAULT_REGION is not set, using ap-south-1")
    
    app.run(host='0.0.0.0', port=80, debug=True)