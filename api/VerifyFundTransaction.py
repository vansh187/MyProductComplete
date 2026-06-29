from fastapi import APIRouter,Depends,BackgroundTasks,status, Header,Request,HTTPException
from utils.auth_dependency import get_current_user
from service.razorpay.Razorypay import Razorpay
from pydantic import BaseModel
import json
from utils.auth_dependency import get_current_user
router=APIRouter()

class VeriFyTransaction(BaseModel):
    razorpay_order_id: str=None
    razorpay_payment_id: str=None
    razorpay_signature: str=None

@router.post("/v1/VerifyFundPayements")
def verifyFundPayments(payload:VeriFyTransaction,background_tasks: BackgroundTasks,currentUser=Depends(get_current_user)):
    userId=currentUser["user_id"]
    razorPay=Razorpay()
    isValid=razorPay.verifyPaymentSignature(payload,userId,background_tasks)
    if isValid:
        ## db call for for updating amount and status
        return{
            "status":200,
            "message" : "Payment Verified"
            
        }
    else:
        return{
            "message" : "Not Verified"    
        }

@router.post("/v1/razorpay-webhook", status_code=status.HTTP_200_OK)
async def verifyRazorPayWebhook(request: Request, x_razorpay_signature: str = Header(None)):
        payload_bytes = await request.body()

        try:
            payload_dict = json.loads(payload_bytes.decode('utf-8'))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed JSON body"
            )

        razorPay = Razorpay()
        isValid = razorPay.verifyWebhookSignature(payload_bytes, x_razorpay_signature)
        print("after verify webhook in verifyfund transaction: " + str(isValid))
        if not isValid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook signature"
            )

        print("before process webhook data in background")
        process_webhook_data_in_background(payload_dict)
        print("after process webhook data in background")
        return {"status": "accepted", "message": "Webhook authenticated and queued"}


def process_webhook_data_in_background(event_data: dict):
        try:
            event_type = event_data.get("event")
            if event_type == "payment.captured":
                payment_entity = event_data["payload"]["payment"]["entity"]
                notes = payment_entity.get("notes") or {}
                user_id = notes.get("user_id")
                print("webhook user_id from notes: " + str(user_id))
                if not user_id:
                    print("Webhook: user_id missing from payment notes, skipping wallet update")
                    return
                razorPay = Razorpay()
                razorPay.verifyPaymentSignatureWebHook(payment_entity, user_id)

        except Exception as ex:
            print(f"Exception occurred while processing webhook: {ex}")
            raise Exception("Exception Occured while verifying webhooks")