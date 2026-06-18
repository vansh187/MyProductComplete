
from service.razorpay.RazorPayMangerService import RazorPayManagerService

class Razorpay:
    def __init__(self):
        pass
    
    
    
    def invokeRazorPayintegration(self,walletLedger,userId):
        razorPayManagerService=RazorPayManagerService()
        razorPaystatus=razorPayManagerService.invokeRazorPayServiceForAddFundsToAccount(walletLedger,userId)
        if razorPaystatus is None:
            return{
                "Message" :"Invalid Request"
                
            }
        return razorPaystatus
        """ elif razorPaystatus.status=="failure":
            return razorPaystatus
        elif razorPaystatus.status =="success":
               return razorPaystatus
                """
            
    def verifyPaymentSignature(self):
        razorPayManagerService=RazorPayManagerService()
        is_authentic= razorPayManagerService.verify_payment_signature( razorpay_order_id="order_Qx9z3M8vP1kL5n",
                                razorpay_payment_id="pay_Qx9z8N2mK3jR6b",
                                razorpay_signature="abcdef1234567890yourgeneratedsignaturehashhere")
        
        if is_authentic:
                print("Payment Signature Verified! Safe to credit user funds.")
        else:
                print("Fraud Warning: Cryptographic signature mismatch!")  