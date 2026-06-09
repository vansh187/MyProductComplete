from database.portfolioPersistence import portfolioPersistence
class portfolioService:

   
    def process_buyer(userId,symbol,quantity,price):
        portfolioPersistence.process_buyer(userId,symbol,quantity,price)


   
    def process_seller(userId,symbol,quantity,price):
        portfolioPersistence.process_seller(userId,symbol,quantity,price)


    def createUserHolding(order,userId):
        portfolioPersistence.createUserHolding(order,userId)

    def updateUserHoldings(order,userId,new_qty,round,new_avg):
        portfolioPersistence.updateUserHoldings(order,userId,new_qty,round,new_avg)